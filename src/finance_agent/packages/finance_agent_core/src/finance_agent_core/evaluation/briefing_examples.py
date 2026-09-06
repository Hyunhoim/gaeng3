from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent import AnswerAdapterResult, execute_answer_request
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAnswerMode,
    BackendStatus,
)
from finance_agent_core.contracts.external_knowledge import CompositeKnowledgeQueryPlan
from finance_agent_core.contracts.independent_hybrid import IndependentHybridPlan
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.evaluation.red_team_e2e import RedTeamDataReference
from finance_agent_core.retrieval import DocumentSourceKind

_SUITE_RESOURCE = "briefing_examples_v1.json"
_SAFE_UNANSWERABLE_STATUSES = {
    BackendStatus.CLARIFICATION,
    BackendStatus.UNSUPPORTED,
    BackendStatus.NOT_FOUND,
    BackendStatus.CANNOT_VERIFY,
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


class BriefingEvaluationMode(StrEnum):
    PRE_ACTIVATION = "pre_activation"
    FINAL_CACHE_ONLY = "final_cache_only"


class BriefingModeExpectation(BriefingModel):
    allowed_statuses: tuple[BackendStatus, ...] = Field(min_length=1)
    require_evidence: bool = False
    require_no_evidence: bool = False

    @model_validator(mode="after")
    def validate_expectation(self) -> BriefingModeExpectation:
        if len(self.allowed_statuses) != len(set(self.allowed_statuses)):
            raise ValueError("allowed briefing statuses must be unique")
        if self.require_evidence == self.require_no_evidence:
            raise ValueError("briefing mode must require evidence or require no evidence")
        if self.require_evidence and BackendStatus.SUCCESS not in self.allowed_statuses:
            raise ValueError("evidence-bearing briefing mode must allow success")
        if self.require_no_evidence and BackendStatus.SUCCESS in self.allowed_statuses:
            raise ValueError("no-evidence briefing mode cannot allow success")
        return self


class BriefingTimeRange(BriefingModel):
    start: date
    end: date

    @model_validator(mode="after")
    def validate_order(self) -> BriefingTimeRange:
        if self.start > self.end:
            raise ValueError("briefing time range is reversed")
        return self


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
    required_external_capabilities: list[KnowledgeCapability] = Field(max_length=5)
    required_time_range: BriefingTimeRange | None = None
    require_db_product_intersection: bool
    mode_expectations: dict[BriefingEvaluationMode, BriefingModeExpectation]

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
        if len(self.required_external_capabilities) != len(
            set(self.required_external_capabilities)
        ):
            raise ValueError("required external capabilities must be unique")
        if set(self.mode_expectations) != set(BriefingEvaluationMode):
            raise ValueError("every briefing case requires both evaluation modes")
        final = self.mode_expectations[BriefingEvaluationMode.FINAL_CACHE_ONLY]
        if self.answerability is BriefingAnswerability.ANSWERABLE:
            if final.allowed_statuses != (BackendStatus.SUCCESS,) or not final.require_evidence:
                raise ValueError("final answerable examples require evidence-backed success")
        elif not final.require_no_evidence:
            raise ValueError("final unanswerable examples require an empty evidence response")
        if self.required_time_range is not None and not self.required_external_capabilities:
            raise ValueError("briefing time range requires an external capability")
        if self.require_db_product_intersection and not self.required_external_capabilities:
            raise ValueError("DB intersection requires an external capability")
        return self


class BriefingExampleSuite(BriefingModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["briefing-examples-v1-8"]
    suite_version: Literal["2.0"]
    status: Literal["official_examples_20260824_current_not_blind"]
    dataset_release_id: Literal["miraeasset-ai-festival-2026-20260824-v5"]
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
    evaluation_mode: BriefingEvaluationMode
    actual_http_status: int
    actual_backend_status: BackendStatus
    actual_interaction_intent: InteractionIntent
    actual_product_families: list[ProductFamily]
    candidate_count: int | None
    evidence_count: int = Field(ge=0)
    returned_product_ids: list[str]
    observed_external_capabilities: list[KnowledgeCapability]
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
    evaluation_mode: BriefingEvaluationMode
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


def _external_requirement_payloads(
    plan: object,
) -> tuple[dict[str, Any], ...]:
    if isinstance(plan, CompositeKnowledgeQueryPlan):
        return tuple(item.model_dump(mode="json") for item in plan.requirements)
    if isinstance(plan, IndependentHybridPlan):
        try:
            payload = json.loads(plan.external_section.subplan.canonical_plan_json)
        except (TypeError, ValueError):
            return ()
        requirements = payload.get("requirements") if isinstance(payload, dict) else None
        if not isinstance(requirements, list):
            return ()
        return tuple(item for item in requirements if isinstance(item, dict))
    return ()


def _required_time_range_preserved(
    required: BriefingTimeRange | None,
    requirements: tuple[dict[str, Any], ...],
) -> bool:
    if required is None:
        return True
    expected = {
        "start": required.start.isoformat(),
        "end": required.end.isoformat(),
    }
    return any(item.get("time_range") == expected for item in requirements)


def _documents_are_cited(response: Any) -> bool:
    cited = {
        evidence_ref for citation in response.citations for evidence_ref in citation.evidence_refs
    }
    return all(
        document.evidence_id in cited
        or any(reference.endswith(f":{document.evidence_id}") for reference in cited)
        for document in response.documents
    )


def _products_are_db_intersection(
    products: list[Any],
    citations: list[Any],
    *,
    allowed_families: set[ProductFamily],
) -> bool:
    cited = {evidence_ref for citation in citations for evidence_ref in citation.evidence_refs}
    family_values = {family.value for family in allowed_families}
    for product in products:
        observed_families = {
            family
            for field in product.fields
            for family in (
                field.source_key.get("product_family"),
                field.source_dataset,
            )
            if family in family_values
        }
        if len(observed_families) != 1:
            return False
        family = observed_families.pop()
        prefix = f"provided:{family}:{product.product_id}:"
        if not any(reference.startswith(prefix) for reference in cited):
            return False
    return True


def evaluate_briefing_example(
    case: BriefingExampleCase,
    adapter: AnswerAdapterResult,
    *,
    latency_ms: float,
    mode: BriefingEvaluationMode = BriefingEvaluationMode.PRE_ACTIVATION,
) -> BriefingExampleCaseResult:
    response = adapter.response
    evidence_count = (
        len(response.products)
        + len(response.comparisons)
        + len(response.aggregates)
        + len(response.documents)
    )
    target_families = set(case.target_families)
    observed_families = set(response.product_families)
    target_family_match = not target_families or (
        observed_families == target_families
        if mode is BriefingEvaluationMode.FINAL_CACHE_ONLY
        else target_families <= observed_families
    )
    intent_match = response.intent in case.expected_interaction_intents
    expectation = case.mode_expectations[mode]
    requirement_payloads = _external_requirement_payloads(response.query_plan)
    observed_external_capabilities = sorted(
        {
            KnowledgeCapability(payload["capability"])
            for payload in requirement_payloads
            if payload.get("capability") in {item.value for item in KnowledgeCapability}
        },
        key=lambda item: item.value,
    )
    evidence_absent = (
        evidence_count == 0
        and not response.citations
        and not response.as_of_dates
        and response.candidate_count in {None, 0}
    )
    required_capabilities_present = set(case.required_external_capabilities) <= set(
        observed_external_capabilities
    )
    external_documents_approved = not case.required_external_capabilities or (
        bool(response.documents)
        and all(
            document.source_kind is DocumentSourceKind.EXTERNAL_APPROVED
            for document in response.documents
        )
        and _documents_are_cited(response)
    )
    time_range_preserved = _required_time_range_preserved(
        case.required_time_range,
        requirement_payloads,
    )
    db_intersection_verified = not case.require_db_product_intersection or (
        bool(response.products)
        and _products_are_db_intersection(
            response.products,
            response.citations,
            allowed_families=set(case.target_families),
        )
    )
    status_allowed = response.status in expectation.allowed_statuses
    evidence_contract = evidence_count > 0 if expectation.require_evidence else evidence_absent
    answer_casefold = response.answer.casefold()
    checks = {
        "http_status_200": adapter.http_status_code == 200,
        "mode_status_allowed": status_allowed,
        "mode_evidence_contract": evidence_contract,
        "product_families_exact": target_family_match,
        "interaction_intent_allowed": intent_match,
        "required_external_capabilities_present": (
            required_capabilities_present if expectation.require_evidence else True
        ),
        "external_documents_approved": (
            external_documents_approved if expectation.require_evidence else True
        ),
        "required_time_range_preserved": (
            time_range_preserved if expectation.require_evidence else True
        ),
        "provided_db_intersection_verified": (
            db_intersection_verified if expectation.require_evidence else True
        ),
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
        evaluation_mode=mode,
        actual_http_status=adapter.http_status_code,
        actual_backend_status=response.status,
        actual_interaction_intent=response.intent,
        actual_product_families=response.product_families,
        candidate_count=response.candidate_count,
        evidence_count=evidence_count,
        returned_product_ids=[product.product_id for product in response.products],
        observed_external_capabilities=observed_external_capabilities,
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
    mode: BriefingEvaluationMode,
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
        if not result.passed:
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
        evaluation_mode=mode,
        suite_sha256=loaded_suite.sha256,
        database_sha256_by_family=database_sha256_by_family,
        summary=summary,
        cases=results,
        interpretation_limits=[
            "설명회에서 공개한 분포 예시이며 실제 평가 문항이나 독립 blind 세트가 아니다.",
            "pre_activation은 승인 corpus 결합 전 CANNOT_VERIFY를 정직하게 허용한다.",
            "final_cache_only는 답변 가능 5건의 DB 교집합·공식문서·필수 capability를 강제한다.",
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
        mode: BriefingEvaluationMode = BriefingEvaluationMode.PRE_ACTIVATION,
    ) -> None:
        self.loaded_suite = loaded_suite
        self.service = service
        self.database_sha256_by_family = database_sha256_by_family
        self.mode = mode

    def run(self, *, generated_at_utc: str | None = None) -> BriefingExampleReport:
        results: list[BriefingExampleCaseResult] = []
        for case in self.loaded_suite.suite.cases:
            request = BackendAgentRequest(request_id=case.id, question=case.question)
            started = time.perf_counter()
            adapter = execute_answer_request(self.service, request)
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            results.append(
                evaluate_briefing_example(
                    case,
                    adapter,
                    latency_ms=latency_ms,
                    mode=self.mode,
                )
            )
        return build_briefing_example_report(
            loaded_suite=self.loaded_suite,
            database_sha256_by_family=self.database_sha256_by_family,
            results=results,
            mode=self.mode,
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
