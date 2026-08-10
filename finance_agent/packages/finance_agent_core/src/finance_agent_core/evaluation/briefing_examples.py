from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent import AnswerAdapterResult, execute_answer_request
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAnswerMode,
    BackendStatus,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.evaluation.red_team_e2e import RedTeamDataReference

_SUITE_RESOURCE = "briefing_examples_v1.json"
_SAFE_UNANSWERABLE_STATUSES = {
    BackendStatus.CLARIFICATION,
    BackendStatus.UNSUPPORTED,
    BackendStatus.NOT_FOUND,
}
_GLOBAL_FORBIDDEN_ANSWER_FRAGMENTS = (
    "/home/",
    "system prompt:",
    "api_key",
    "authorization: bearer",
    "select * from",
)


class BriefingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BriefingAnswerability(StrEnum):
    ANSWERABLE = "answerable"
    UNANSWERABLE = "unanswerable"


class BriefingDifficulty(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    NOT_APPLICABLE = "not_applicable"


class BriefingSource(BriefingModel):
    title: str = Field(min_length=1, max_length=300)
    artifact_name: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    interpretation: Literal["official_distribution_example_not_evaluation_item"]


class BriefingExampleCase(BriefingModel):
    id: str = Field(pattern=r"^briefing-examples-v1-[0-9]{3}$")
    answerability: BriefingAnswerability
    difficulty: BriefingDifficulty
    question: str = Field(min_length=1, max_length=2000)
    target_families: list[ProductFamily] = Field(max_length=4)
    expected_interaction_intents: list[InteractionIntent] = Field(min_length=1)
    required_capabilities: list[str] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_answerability_contract(self) -> BriefingExampleCase:
        if self.answerability is BriefingAnswerability.ANSWERABLE:
            if self.difficulty is BriefingDifficulty.NOT_APPLICABLE:
                raise ValueError("answerable examples require an explicit difficulty")
            if not self.target_families:
                raise ValueError("answerable examples require target product families")
        elif self.difficulty is not BriefingDifficulty.NOT_APPLICABLE:
            raise ValueError("unanswerable examples use not_applicable difficulty")
        if len(self.target_families) != len(set(self.target_families)):
            raise ValueError("target families must be unique")
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("required capabilities must be unique")
        return self


class BriefingExampleSuite(BriefingModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["briefing-examples-v1-8"]
    suite_version: Literal["1.0"]
    status: Literal["official_examples_public_not_blind"]
    source: BriefingSource
    data: dict[ProductFamily, RedTeamDataReference]
    cases: list[BriefingExampleCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_suite_contract(self) -> BriefingExampleSuite:
        if set(self.data) != set(ProductFamily):
            raise ValueError("briefing suite must pin all four normalized databases")
        if len(self.cases) != 8:
            raise ValueError("briefing-examples-v1 must contain exactly eight examples")
        expected_ids = [
            f"briefing-examples-v1-{index:03d}" for index in range(1, len(self.cases) + 1)
        ]
        if [case.id for case in self.cases] != expected_ids:
            raise ValueError("briefing example IDs must be ordered and contiguous")
        questions = [" ".join(case.question.split()).casefold() for case in self.cases]
        if len(questions) != len(set(questions)):
            raise ValueError("briefing example questions must be unique")
        answerability = Counter(case.answerability for case in self.cases)
        if answerability != Counter(
            {
                BriefingAnswerability.ANSWERABLE: 5,
                BriefingAnswerability.UNANSWERABLE: 3,
            }
        ):
            raise ValueError("briefing suite requires five answerable and three unanswerable")
        answerable_difficulty = Counter(
            case.difficulty
            for case in self.cases
            if case.answerability is BriefingAnswerability.ANSWERABLE
        )
        if answerable_difficulty != Counter(
            {
                BriefingDifficulty.LOW: 1,
                BriefingDifficulty.MEDIUM: 2,
                BriefingDifficulty.HIGH: 2,
            }
        ):
            raise ValueError("answerable briefing examples require the official 1/2/2 split")
        return self


class LoadedBriefingExampleSuite(BriefingModel):
    suite: BriefingExampleSuite
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_name: str


class BriefingExampleCaseResult(BriefingModel):
    id: str
    answerability: BriefingAnswerability
    difficulty: BriefingDifficulty
    required_capabilities: list[str]
    actual_http_status: int
    actual_backend_status: BackendStatus
    actual_interaction_intent: InteractionIntent
    actual_product_families: list[ProductFamily]
    candidate_count: int | None
    evidence_count: int = Field(ge=0)
    answer_mode: BackendAnswerMode
    latency_ms: float = Field(ge=0)
    checks: dict[str, bool]
    violations: list[str]
    passed: bool


class BriefingExampleSummary(BriefingModel):
    total: int
    passed: int
    strict_accuracy: float
    answerable_total: int
    answerable_executed: int
    answerable_execution_rate: float
    unanswerable_total: int
    unanswerable_safely_handled: int
    unanswerable_safety_rate: float
    unsafe_unanswerable_executions: int
    status_counts: dict[str, int]
    capability_gap_counts: dict[str, int]
    perfect: bool


class BriefingExampleReport(BriefingModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: str
    generated_at_utc: str
    suite_id: str
    suite_version: str
    suite_sha256: str
    database_sha256_by_family: dict[str, str]
    summary: BriefingExampleSummary
    cases: list[BriefingExampleCaseResult]
    interpretation_limits: list[str]


class RoutedAnswerService(Protocol):
    def answer(self, question: str, request_id: str): ...

    @property
    def router(self): ...


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_briefing_example_suite() -> LoadedBriefingExampleSuite:
    resource = files("finance_agent_core.evaluation.suites").joinpath(_SUITE_RESOURCE)
    raw = resource.read_bytes()
    return LoadedBriefingExampleSuite(
        suite=BriefingExampleSuite.model_validate_json(raw),
        sha256=_sha256_bytes(raw),
        resource_name=_SUITE_RESOURCE,
    )


def verify_briefing_example_databases(
    suite: BriefingExampleSuite,
    database_paths: Mapping[ProductFamily | str, str | Path],
) -> dict[str, str]:
    normalized = {ProductFamily(family): Path(path) for family, path in database_paths.items()}
    if set(normalized) != set(ProductFamily):
        raise ValueError("database paths must configure all four product families")
    observed: dict[str, str] = {}
    for family in ProductFamily:
        path = normalized[family]
        digest = _sha256_bytes(path.read_bytes())
        expected = suite.data[family].database_sha256
        if digest != expected:
            raise ValueError(
                f"{family.value} database SHA-256 differs: expected {expected}, got {digest}"
            )
        manifest_path = path.with_suffix(f"{path.suffix}.manifest.json")
        manifest_digest = _sha256_bytes(manifest_path.read_bytes())
        expected_manifest = suite.data[family].manifest_sha256
        if manifest_digest != expected_manifest:
            raise ValueError(
                f"{family.value} manifest SHA-256 differs: "
                f"expected {expected_manifest}, got {manifest_digest}"
            )
        observed[family.value] = digest
    return observed


def evaluate_briefing_example(
    case: BriefingExampleCase,
    adapter: AnswerAdapterResult,
    *,
    latency_ms: float,
) -> BriefingExampleCaseResult:
    response = adapter.response
    evidence_count = (
        len(response.products)
        + len(response.comparisons)
        + len(response.aggregates)
        + len(response.documents)
    )
    target_family_match = not case.target_families or bool(
        set(response.product_families) & set(case.target_families)
    )
    intent_match = response.intent in case.expected_interaction_intents
    if case.answerability is BriefingAnswerability.ANSWERABLE:
        answerability_check = response.status is BackendStatus.SUCCESS and evidence_count > 0
    else:
        answerability_check = (
            response.status in _SAFE_UNANSWERABLE_STATUSES
            and evidence_count == 0
            and response.candidate_count in {None, 0}
        )
    answer_casefold = response.answer.casefold()
    checks = {
        "http_status_200": adapter.http_status_code == 200,
        "answerability_contract": answerability_check,
        "target_family_reached": target_family_match,
        "interaction_intent_allowed": intent_match,
        "forbidden_answer_absent": not any(
            fragment.casefold() in answer_casefold
            for fragment in _GLOBAL_FORBIDDEN_ANSWER_FRAGMENTS
        ),
        "no_backend_error": response.error is None,
        "request_id_preserved": response.request_id == case.id,
    }
    violations = [name for name, passed in checks.items() if not passed]
    return BriefingExampleCaseResult(
        id=case.id,
        answerability=case.answerability,
        difficulty=case.difficulty,
        required_capabilities=case.required_capabilities,
        actual_http_status=adapter.http_status_code,
        actual_backend_status=response.status,
        actual_interaction_intent=response.intent,
        actual_product_families=response.product_families,
        candidate_count=response.candidate_count,
        evidence_count=evidence_count,
        answer_mode=response.answer_mode,
        latency_ms=latency_ms,
        checks=checks,
        violations=violations,
        passed=all(checks.values()),
    )


def build_briefing_example_report(
    *,
    loaded_suite: LoadedBriefingExampleSuite,
    database_sha256_by_family: dict[str, str],
    results: list[BriefingExampleCaseResult],
    generated_at_utc: str | None = None,
) -> BriefingExampleReport:
    answerable = [
        result for result in results if result.answerability is BriefingAnswerability.ANSWERABLE
    ]
    unanswerable = [
        result for result in results if result.answerability is BriefingAnswerability.UNANSWERABLE
    ]
    answerable_executed = sum(
        result.actual_backend_status is BackendStatus.SUCCESS and result.evidence_count > 0
        for result in answerable
    )
    unanswerable_safe = sum(
        result.actual_backend_status in _SAFE_UNANSWERABLE_STATUSES
        and result.evidence_count == 0
        and result.candidate_count in {None, 0}
        for result in unanswerable
    )
    capability_gap_counts: Counter[str] = Counter()
    for case, result in zip(loaded_suite.suite.cases, results, strict=True):
        if not result.checks["answerability_contract"]:
            capability_gap_counts.update(case.required_capabilities)
    passed = sum(result.passed for result in results)
    total = len(results)
    summary = BriefingExampleSummary(
        total=total,
        passed=passed,
        strict_accuracy=round(passed / total, 6),
        answerable_total=len(answerable),
        answerable_executed=answerable_executed,
        answerable_execution_rate=round(answerable_executed / len(answerable), 6),
        unanswerable_total=len(unanswerable),
        unanswerable_safely_handled=unanswerable_safe,
        unanswerable_safety_rate=round(unanswerable_safe / len(unanswerable), 6),
        unsafe_unanswerable_executions=len(unanswerable) - unanswerable_safe,
        status_counts=dict(
            sorted(Counter(result.actual_backend_status.value for result in results).items())
        ),
        capability_gap_counts=dict(sorted(capability_gap_counts.items())),
        perfect=passed == total,
    )
    return BriefingExampleReport(
        report_id="briefing-examples-v1-current",
        generated_at_utc=generated_at_utc or datetime.now(UTC).isoformat(),
        suite_id=loaded_suite.suite.suite_id,
        suite_version=loaded_suite.suite.suite_version,
        suite_sha256=loaded_suite.sha256,
        database_sha256_by_family=database_sha256_by_family,
        summary=summary,
        cases=results,
        interpretation_limits=[
            "설명회에서 공개한 분포 예시이며 실제 평가 문항이나 독립 blind 세트가 아니다.",
            "현재 관측은 결정론적 Router·검색·검증 경로만 사용하며 LLM 생성 품질이 아니다.",
            "공개 예시를 본 뒤의 수정 결과는 최초 관측과 분리해 기록해야 한다.",
            "답변 가능 예시는 성공과 근거 존재만 확인하며 최종 금융 정답의 완결성은 별도 평가한다.",
        ],
    )


class BriefingExampleRunner:
    def __init__(
        self,
        *,
        loaded_suite: LoadedBriefingExampleSuite,
        service: RoutedAnswerService,
        database_sha256_by_family: dict[str, str],
    ) -> None:
        self.loaded_suite = loaded_suite
        self.service = service
        self.database_sha256_by_family = database_sha256_by_family

    def run(self, *, generated_at_utc: str | None = None) -> BriefingExampleReport:
        results: list[BriefingExampleCaseResult] = []
        for case in self.loaded_suite.suite.cases:
            request = BackendAgentRequest(request_id=case.id, question=case.question)
            started = time.perf_counter()
            adapter = execute_answer_request(self.service, request)
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            results.append(evaluate_briefing_example(case, adapter, latency_ms=latency_ms))
        return build_briefing_example_report(
            loaded_suite=self.loaded_suite,
            database_sha256_by_family=self.database_sha256_by_family,
            results=results,
            generated_at_utc=generated_at_utc,
        )


def report_semantic_sha256(report: BriefingExampleReport) -> str:
    payload = report.model_dump(mode="json")
    payload.pop("generated_at_utc")
    for case in payload["cases"]:
        case.pop("latency_ms")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return _sha256_bytes(encoded)
