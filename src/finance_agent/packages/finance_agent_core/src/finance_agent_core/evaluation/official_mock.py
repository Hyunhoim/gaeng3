from __future__ import annotations

import json
import math
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent import (
    AnswerAdapterResult,
    execute_answer_request,
    official_response_from_backend,
)
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAnswerMode,
    BackendStatus,
)
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.evaluation.briefing_examples import (
    BriefingAnswerability,
    BriefingDifficulty,
)
from finance_agent_core.evaluation.red_team_e2e import (
    ProviderCallSnapshot,
    ProviderTelemetry,
    RedTeamAttackClass,
    RedTeamCaseResult,
    RedTeamDataReference,
    RedTeamExpectation,
    _evaluate_case,
)

_SUITE_RESOURCE = "official_mock_v1_30.json"
_SAFE_UNANSWERABLE_STATUSES = {
    BackendStatus.CLARIFICATION,
    BackendStatus.UNSUPPORTED,
    BackendStatus.NOT_FOUND,
}
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


class OfficialMockModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OfficialMockSource(OfficialMockModel):
    title: str = Field(min_length=1, max_length=300)
    artifact_name: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    interpretation: Literal["official_distribution_only_not_evaluation_items"]


class OfficialMockCase(OfficialMockModel):
    id: str = Field(pattern=r"^official-mock-v1-[0-9]{3}$")
    difficulty: BriefingDifficulty
    answerability: BriefingAnswerability
    coverage_family: ProductFamily
    attack_class: RedTeamAttackClass
    source_case_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=2000)
    expectation: RedTeamExpectation

    @model_validator(mode="after")
    def validate_answerability(self) -> OfficialMockCase:
        if self.difficulty is BriefingDifficulty.NOT_APPLICABLE:
            raise ValueError("official mock cases require low, medium, or high difficulty")
        if self.answerability is BriefingAnswerability.ANSWERABLE:
            if self.expectation.backend_status is not BackendStatus.SUCCESS:
                raise ValueError("answerable mock cases must expect success")
        elif self.expectation.backend_status not in _SAFE_UNANSWERABLE_STATUSES:
            raise ValueError("unanswerable mock cases require a safe control status")
        return self


class OfficialMockSuite(OfficialMockModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["official-mock-v1-30"]
    suite_version: Literal["1.0"]
    status: Literal["public_official_shape_mock_not_blind"]
    is_blind: Literal[False]
    author_role: Literal["ai_engineering"]
    source: OfficialMockSource
    source_suite_ids: list[str] = Field(min_length=1)
    data: dict[ProductFamily, RedTeamDataReference]
    cases: list[OfficialMockCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_suite_contract(self) -> OfficialMockSuite:
        if set(self.data) != set(ProductFamily):
            raise ValueError("official mock suite must pin all four normalized databases")
        if len(self.cases) != 30:
            raise ValueError("official-mock-v1 must contain exactly 30 cases")
        expected_ids = [f"official-mock-v1-{index:03d}" for index in range(1, len(self.cases) + 1)]
        if [case.id for case in self.cases] != expected_ids:
            raise ValueError("official mock IDs must be unique, ordered, and contiguous")
        questions = [" ".join(case.question.split()).casefold() for case in self.cases]
        if len(questions) != len(set(questions)):
            raise ValueError("official mock questions must be unique")
        if Counter(case.difficulty for case in self.cases) != Counter(
            {
                BriefingDifficulty.LOW: 10,
                BriefingDifficulty.MEDIUM: 10,
                BriefingDifficulty.HIGH: 10,
            }
        ):
            raise ValueError("official mock requires ten low, medium, and high cases")
        if Counter(case.answerability for case in self.cases) != Counter(
            {
                BriefingAnswerability.ANSWERABLE: 25,
                BriefingAnswerability.UNANSWERABLE: 5,
            }
        ):
            raise ValueError("official mock requires 25 answerable and 5 unanswerable cases")
        answerable_families = Counter(
            case.coverage_family
            for case in self.cases
            if case.answerability is BriefingAnswerability.ANSWERABLE
        )
        if answerable_families != Counter(
            {
                ProductFamily.OVERSEAS_ETP: 7,
                ProductFamily.DOMESTIC_ETP: 6,
                ProductFamily.BOND: 6,
                ProductFamily.FUND: 6,
            }
        ):
            raise ValueError("official mock answerable cases must preserve 7/6/6/6 coverage")
        return self


class LoadedOfficialMockSuite(OfficialMockModel):
    suite: OfficialMockSuite
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_name: str


class OfficialMockCaseResult(OfficialMockModel):
    id: str
    difficulty: BriefingDifficulty
    answerability: BriefingAnswerability
    coverage_family: ProductFamily
    source_case_id: str
    system: RedTeamCaseResult
    official_checks: dict[str, bool]
    official_contract_passed: bool
    passed: bool


class OfficialMockSummary(OfficialMockModel):
    total: int
    passed: int
    strict_accuracy: float
    answerable_total: int
    answerable_passed: int
    answerable_accuracy: float
    unanswerable_total: int
    unanswerable_safely_handled: int
    unanswerable_safety_rate: float
    safety_passed: int
    safety_pass_rate: float
    evidence_passed: int
    evidence_pass_rate: float
    official_contract_passed: int
    official_contract_pass_rate: float
    llm_answer_eligible: int
    llm_grounded: int
    llm_grounded_rate: float | None
    fallback_count: int
    fallback_rate: float | None
    difficulty_accuracy: dict[str, float]
    family_accuracy: dict[str, float]
    latency_ms: dict[str, float]
    provider_call_contract_passed: bool
    perfect: bool


class OfficialMockReport(OfficialMockModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: str
    generated_at_utc: str
    profile: Literal["expected", "local_test"]
    suite_id: str
    suite_version: str
    suite_sha256: str
    database_sha256_by_family: dict[str, str]
    model: str | None
    provider_calls: ProviderCallSnapshot
    expected_provider_calls: dict[str, int]
    summary: OfficialMockSummary
    cases: list[OfficialMockCaseResult]
    interpretation_limits: list[str]


class RoutedAnswerService(Protocol):
    def answer(self, question: str, request_id: str): ...

    @property
    def router(self): ...


def _sha256_bytes(payload: bytes) -> str:
    import hashlib

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


def load_official_mock_suite() -> LoadedOfficialMockSuite:
    resource = files("finance_agent_core.evaluation.suites").joinpath(_SUITE_RESOURCE)
    raw = resource.read_bytes()
    return LoadedOfficialMockSuite(
        suite=OfficialMockSuite.model_validate_json(raw),
        sha256=_sha256_bytes(raw),
        resource_name=_SUITE_RESOURCE,
    )


def verify_official_mock_databases(
    suite: OfficialMockSuite,
    database_paths: Mapping[ProductFamily | str, str | Path],
) -> dict[str, str]:
    normalized = {ProductFamily(family): Path(path) for family, path in database_paths.items()}
    if set(normalized) != set(ProductFamily):
        raise ValueError("database paths must configure all four product families")
    observed: dict[str, str] = {}
    for family in ProductFamily:
        path = normalized[family]
        digest = _sha256_bytes(path.read_bytes())
        if digest != suite.data[family].database_sha256:
            raise ValueError(f"{family.value} database SHA-256 differs")
        manifest = path.with_suffix(f"{path.suffix}.manifest.json")
        if _sha256_bytes(manifest.read_bytes()) != suite.data[family].manifest_sha256:
            raise ValueError(f"{family.value} manifest SHA-256 differs")
        observed[family.value] = digest
    return observed


def _official_checks(
    case: OfficialMockCase,
    adapter: AnswerAdapterResult,
) -> dict[str, bool]:
    official = official_response_from_backend(
        question_id=case.id,
        question=case.question,
        response=adapter.response,
    )
    payload = official.model_dump(mode="json")
    decoded_objects: dict[str, object] = {}
    for field in ("retrieved_context", "think_trace"):
        try:
            decoded_objects[field] = json.loads(payload[field])
        except (json.JSONDecodeError, TypeError):
            decoded_objects[field] = None
    serialized = json.dumps(payload, ensure_ascii=False).casefold()
    return {
        "exact_five_fields": set(payload) == _OFFICIAL_FIELDS,
        "all_fields_are_strings": all(isinstance(value, str) for value in payload.values()),
        "question_id_preserved": official.question_id == case.id,
        "question_preserved": official.question == case.question,
        "retrieved_context_is_json_object": isinstance(decoded_objects["retrieved_context"], dict),
        "think_trace_is_json_object": isinstance(decoded_objects["think_trace"], dict),
        "answer_is_nonempty": bool(official.answer.strip()),
        "forbidden_public_fragments_absent": not any(
            fragment in serialized for fragment in _FORBIDDEN_PUBLIC_FRAGMENTS
        ),
    }


def evaluate_official_mock_case(
    case: OfficialMockCase,
    adapter: AnswerAdapterResult,
    *,
    latency_ms: float,
    provider_plan: QueryPlan | None = None,
) -> OfficialMockCaseResult:
    system = _evaluate_case(
        case,  # type: ignore[arg-type]
        adapter,
        latency_ms,
        provider_plan=provider_plan,
    )
    official_checks = _official_checks(case, adapter)
    official_contract_passed = all(official_checks.values())
    return OfficialMockCaseResult(
        id=case.id,
        difficulty=case.difficulty,
        answerability=case.answerability,
        coverage_family=case.coverage_family,
        source_case_id=case.source_case_id,
        system=system,
        official_checks=official_checks,
        official_contract_passed=official_contract_passed,
        passed=system.passed and official_contract_passed,
    )


def _accuracy_by(
    results: Sequence[OfficialMockCaseResult],
    attribute: Literal["difficulty", "coverage_family"],
) -> dict[str, float]:
    grouped: dict[str, list[bool]] = {}
    for result in results:
        value: StrEnum = getattr(result, attribute)
        grouped.setdefault(value.value, []).append(result.passed)
    return {key: round(sum(values) / len(values), 6) for key, values in sorted(grouped.items())}


def _expected_provider_calls(
    suite: OfficialMockSuite,
    profile: Literal["expected", "local_test"],
) -> dict[str, int]:
    query_plan_calls = 0
    if profile == "local_test":
        for case in suite.cases:
            expectation = case.expectation
            if expectation.backend_status not in {BackendStatus.SUCCESS, BackendStatus.NOT_FOUND}:
                continue
            if expectation.query_plan_intent is Intent.SEARCH:
                query_plan_calls += 1
            # Cross-family SEARCH is compiled from server-owned family plans.
            # The LLM only explains each verified family result afterward.
    answer_calls = sum(
        (
            len(case.expectation.product_families)
            if len(case.expectation.product_families) > 1
            else 1
        )
        for case in suite.cases
        if case.expectation.llm_answer_eligible
    )
    return {"query_plan_calls": query_plan_calls, "answer_calls": answer_calls}


def build_official_mock_report(
    *,
    loaded_suite: LoadedOfficialMockSuite,
    profile: Literal["expected", "local_test"],
    database_sha256_by_family: dict[str, str],
    model: str | None,
    provider_calls: ProviderCallSnapshot,
    results: list[OfficialMockCaseResult],
    generated_at_utc: str | None = None,
) -> OfficialMockReport:
    total = len(results)
    passed = sum(result.passed for result in results)
    answerable = [
        result for result in results if result.answerability is BriefingAnswerability.ANSWERABLE
    ]
    unanswerable = [
        result for result in results if result.answerability is BriefingAnswerability.UNANSWERABLE
    ]
    safely_handled = sum(
        result.passed
        and result.system.actual_backend_status in _SAFE_UNANSWERABLE_STATUSES
        and result.system.actual_candidate_count in {None, 0}
        for result in unanswerable
    )
    safety_passed = sum(result.system.safety_passed for result in results)
    evidence_passed = sum(result.system.evidence_passed for result in results)
    official_passed = sum(result.official_contract_passed for result in results)
    eligible = sum(result.system.expected.llm_answer_eligible for result in results)
    grounded = sum(
        result.system.expected.llm_answer_eligible
        and result.system.answer_mode is BackendAnswerMode.LLM_GROUNDED
        for result in results
    )
    fallback = sum(
        result.system.expected.llm_answer_eligible
        and result.system.answer_mode is BackendAnswerMode.DETERMINISTIC_FALLBACK
        for result in results
    )
    expected_calls = _expected_provider_calls(loaded_suite.suite, profile)
    provider_call_contract = (
        provider_calls.query_plan_calls == expected_calls["query_plan_calls"]
        and provider_calls.answer_calls == expected_calls["answer_calls"]
    )
    latencies = [result.system.latency_ms for result in results]
    summary = OfficialMockSummary(
        total=total,
        passed=passed,
        strict_accuracy=round(passed / total, 6),
        answerable_total=len(answerable),
        answerable_passed=sum(result.passed for result in answerable),
        answerable_accuracy=round(sum(result.passed for result in answerable) / len(answerable), 6),
        unanswerable_total=len(unanswerable),
        unanswerable_safely_handled=safely_handled,
        unanswerable_safety_rate=round(safely_handled / len(unanswerable), 6),
        safety_passed=safety_passed,
        safety_pass_rate=round(safety_passed / total, 6),
        evidence_passed=evidence_passed,
        evidence_pass_rate=round(evidence_passed / total, 6),
        official_contract_passed=official_passed,
        official_contract_pass_rate=round(official_passed / total, 6),
        llm_answer_eligible=eligible,
        llm_grounded=grounded,
        llm_grounded_rate=None if not eligible else round(grounded / eligible, 6),
        fallback_count=fallback,
        fallback_rate=None if not eligible else round(fallback / eligible, 6),
        difficulty_accuracy=_accuracy_by(results, "difficulty"),
        family_accuracy=_accuracy_by(results, "coverage_family"),
        latency_ms={
            "min": round(min(latencies), 3),
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 3),
        },
        provider_call_contract_passed=provider_call_contract,
        perfect=passed == total and provider_call_contract,
    )
    timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
    return OfficialMockReport(
        report_id=f"official-mock-v1-30-{profile}",
        generated_at_utc=timestamp,
        profile=profile,
        suite_id=loaded_suite.suite.suite_id,
        suite_version=loaded_suite.suite.suite_version,
        suite_sha256=loaded_suite.sha256,
        database_sha256_by_family=database_sha256_by_family,
        model=model,
        provider_calls=provider_calls,
        expected_provider_calls=expected_calls,
        summary=summary,
        cases=results,
        interpretation_limits=[
            "설명회에서 안내된 10/10/10 난이도와 답변 불가 5개 분포만 모사했다.",
            "AI 담당자가 기존 질문·DB·구현을 본 뒤 구성한 공개 모의평가이며 blind가 아니다.",
            "재사용한 회귀 질문과 정답은 공식 평가 문항 또는 공식 정답이 아니다.",
            "로컬 Qwen 결과는 HyperCLOVA X 또는 공모전 평가 성능을 대변하지 않는다.",
            "fallback은 안전성 성공으로 보되 생성 품질 성공과 별도로 집계한다.",
        ],
    )


class OfficialMockRunner:
    def __init__(
        self,
        *,
        loaded_suite: LoadedOfficialMockSuite,
        services: Mapping[ProductFamily, RoutedAnswerService],
        profile: Literal["expected", "local_test"],
        database_sha256_by_family: dict[str, str],
        telemetry: ProviderTelemetry,
        model: str | None,
    ) -> None:
        if set(services) != set(ProductFamily):
            raise ValueError("runner requires one service mapping per coverage family")
        self.loaded_suite = loaded_suite
        self.services = services
        self.profile = profile
        self.database_sha256_by_family = database_sha256_by_family
        self.telemetry = telemetry
        self.model = model

    def run(self) -> OfficialMockReport:
        results: list[OfficialMockCaseResult] = []
        for case in self.loaded_suite.suite.cases:
            request = BackendAgentRequest(request_id=case.id, question=case.question)
            started = time.perf_counter()
            adapter = execute_answer_request(self.services[case.coverage_family], request)
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            results.append(
                evaluate_official_mock_case(
                    case,
                    adapter,
                    latency_ms=latency_ms,
                    provider_plan=self.telemetry.query_plan(case.id),
                )
            )
        return build_official_mock_report(
            loaded_suite=self.loaded_suite,
            profile=self.profile,
            database_sha256_by_family=self.database_sha256_by_family,
            model=self.model,
            provider_calls=self.telemetry.snapshot(),
            results=results,
        )
