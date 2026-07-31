from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent import AnswerAdapterResult, execute_answer_request
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAgentResponse,
    BackendAnswerMode,
    BackendStatus,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent

DOMAIN_QA_SPEC_RESOURCE = "domain_qa_dev_v1.json"
_QUESTION_HEADERS = {
    "번호",
    "상품군",
    "사용자 질문",
    "질문을 하는 상황 또는 목적",
    "기대하는 처리",
    "금융적으로 주의할 점 또는 메모",
}
_REVIEW_HEADERS = {
    "번호",
    "상품군",
    "사용자 질문",
    "원래 기대 처리",
    "검토 분류",
    "현재 권장 처리",
    "현재 데이터 지원",
    "평가 경로",
    "심각도",
    "검토 근거",
    "후속 조치",
}
_GLOBAL_FORBIDDEN_ANSWER_FRAGMENTS = (
    "/home/",
    "system prompt:",
    "api_key",
    "authorization: bearer",
    "select * from",
)


class DomainQAModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DomainQAEvaluationPath(StrEnum):
    SEARCH = "SEARCH"
    CLARIFY = "CLARIFY"
    UNSUPPORTED = "UNSUPPORTED"
    DOCUMENT_RAG = "DOCUMENT_RAG"
    EXTERNAL_POLICY = "EXTERNAL_POLICY"
    EXTERNAL_DATA = "EXTERNAL_DATA"


class DomainQAReviewClass(StrEnum):
    ACCEPTED = "채택 후보"
    RELABELED = "라벨 수정 후 채택"
    DOCUMENT_RAG = "문서 RAG 후보"
    EXTERNAL_REVIEW = "외부 기준 확인"


class DomainQASeverity(StrEnum):
    LOW = "낮음"
    MEDIUM = "중간"
    HIGH = "높음"


class BehavioralTestType(StrEnum):
    """CheckList-compatible behavioral test taxonomy."""

    MINIMUM_FUNCTIONALITY = "minimum_functionality"
    INVARIANCE = "invariance"
    DIRECTIONAL_EXPECTATION = "directional_expectation"


class DomainQAGoldLevel(StrEnum):
    ROUTE_ONLY = "route_only"
    ORACLE_PENDING = "oracle_pending"
    DEPENDENCY_PENDING = "dependency_pending"


class DomainQADataReference(DomainQAModel):
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DomainQACountContract(DomainQAModel):
    product_group: dict[str, int]
    review_class: dict[str, int]
    recommended_action: dict[str, int]
    data_support: dict[str, int]
    evaluation_path: dict[str, int]
    severity: dict[str, int]


class DomainQASpec(DomainQAModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["domain-qa-dev-v1-40"]
    suite_version: Literal["1.0"]
    status: Literal["financial_domain_development_not_blind"]
    author_role: Literal["financial_domain"]
    reviewer_role: Literal["ai_engineering"]
    source_questions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_csv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_count: int = Field(gt=0)
    expected_counts: DomainQACountContract
    family_overrides: dict[str, list[ProductFamily] | None]
    data: dict[ProductFamily, DomainQADataReference]

    @model_validator(mode="after")
    def validate_spec(self) -> DomainQASpec:
        if set(self.data) != set(ProductFamily):
            raise ValueError("domain QA spec must pin all four normalized databases")
        invalid_overrides = [
            case_id for case_id in self.family_overrides if re.fullmatch(r"Q\d{3}", case_id) is None
        ]
        if invalid_overrides:
            raise ValueError(f"invalid family override IDs: {invalid_overrides}")
        return self


class DomainQACase(DomainQAModel):
    id: str = Field(pattern=r"^Q\d{3}$")
    source_product_group: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=2000)
    scenario: str = Field(min_length=1, max_length=2000)
    financial_note: str = Field(min_length=1, max_length=3000)
    original_expected_action: str = Field(min_length=1, max_length=100)
    review_class: DomainQAReviewClass
    recommended_action: str = Field(min_length=1, max_length=100)
    data_support: str = Field(min_length=1, max_length=100)
    evaluation_path: DomainQAEvaluationPath
    severity: DomainQASeverity
    rationale: str = Field(min_length=1, max_length=3000)
    next_action: str = Field(min_length=1, max_length=3000)
    behavioral_test_type: BehavioralTestType
    parent_case_id: str | None = Field(default=None, pattern=r"^Q\d{3}$")
    capability: str = Field(min_length=1, max_length=100)
    expected_interaction_intents: list[InteractionIntent] = Field(min_length=1)
    allowed_backend_statuses: list[BackendStatus] = Field(min_length=1)
    expected_product_families: list[ProductFamily] | None
    require_control: bool
    gold_level: DomainQAGoldLevel

    @model_validator(mode="after")
    def validate_behavioral_contract(self) -> DomainQACase:
        if self.behavioral_test_type is BehavioralTestType.MINIMUM_FUNCTIONALITY:
            if self.parent_case_id is not None:
                raise ValueError("minimum-functionality cases cannot have a parent")
        elif self.parent_case_id is None:
            raise ValueError("augmented behavioral cases require a parent_case_id")
        executable_statuses = {BackendStatus.SUCCESS, BackendStatus.NOT_FOUND}
        if self.require_control and executable_statuses & set(self.allowed_backend_statuses):
            raise ValueError("control cases cannot allow executable Backend statuses")
        if not self.require_control and not (
            executable_statuses & set(self.allowed_backend_statuses)
        ):
            raise ValueError("executable cases require success or not_found")
        if self.gold_level is DomainQAGoldLevel.DEPENDENCY_PENDING and not self.require_control:
            raise ValueError("dependency-pending cases must remain control-only")
        return self


class DomainQASuite(DomainQAModel):
    schema_version: Literal["1.0"]
    suite_id: str
    suite_version: str
    status: Literal["financial_domain_development_not_blind"]
    source_questions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_csv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: list[DomainQACase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cases(self) -> DomainQASuite:
        expected_ids = [f"Q{index:03d}" for index in range(1, len(self.cases) + 1)]
        if [case.id for case in self.cases] != expected_ids:
            raise ValueError("domain QA IDs must be unique, ordered, and contiguous")
        normalized = [_normalize_question(case.question) for case in self.cases]
        if len(normalized) != len(set(normalized)):
            raise ValueError("domain QA questions must be unique after normalization")
        return self


class LoadedDomainQASuite(DomainQAModel):
    spec: DomainQASpec
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    suite: DomainQASuite


class DomainQACaseResult(DomainQAModel):
    id: str
    source_product_group: str
    evaluation_path: DomainQAEvaluationPath
    severity: DomainQASeverity
    capability: str
    behavioral_test_type: BehavioralTestType
    gold_level: DomainQAGoldLevel
    expected_interaction_intents: list[InteractionIntent]
    allowed_backend_statuses: list[BackendStatus]
    expected_product_families: list[ProductFamily] | None
    actual_http_status: int
    actual_backend_status: BackendStatus
    actual_interaction_intent: InteractionIntent
    actual_product_families: list[ProductFamily]
    answer_mode: BackendAnswerMode
    fallback_used: bool
    candidate_count: int | None
    evidence_count: int = Field(ge=0)
    citation_count: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    plan_sha256: str | None
    checks: dict[str, bool]
    violations: list[str]
    route_passed: bool
    safety_passed: bool
    evidence_passed: bool
    answer_passed: bool
    strict_passed: bool


class DomainQASummary(DomainQAModel):
    total: int
    passed: int
    strict_accuracy: float
    route_passed: int
    route_pass_rate: float
    safety_passed: int
    safety_pass_rate: float
    evidence_passed: int
    evidence_pass_rate: float
    answer_passed: int
    answer_pass_rate: float
    dependency_pending: int
    oracle_gold_pending: int
    actual_status_counts: dict[str, int]
    test_type_counts: dict[str, int]
    by_evaluation_path: dict[str, dict[str, int | float]]
    by_product_group: dict[str, dict[str, int | float]]
    by_severity: dict[str, dict[str, int | float]]
    by_capability: dict[str, dict[str, int | float]]
    failure_taxonomy: dict[str, int]
    latency_ms: dict[str, float]
    perfect: bool


class DomainQAReport(DomainQAModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^domain-qa-[a-z0-9-]+$",
    )
    generated_at_utc: str
    profile: Literal["deterministic_current"]
    suite_id: str
    suite_version: str
    suite_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_questions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_csv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_sha256_by_family: dict[str, str]
    summary: DomainQASummary
    cases: list[DomainQACaseResult]
    interpretation_limits: list[str]


class RoutedAnswerService(Protocol):
    @property
    def router(self): ...

    def answer(self, question: str, request_id: str): ...


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return _sha256_bytes(encoded)


def _normalize_question(question: str) -> str:
    return re.sub(r"[\W_]+", "", question.casefold())


def _read_csv(path: Path, required_headers: set[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        missing = required_headers - headers
        if missing:
            raise ValueError(f"{path.name}: missing CSV headers {sorted(missing)}")
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    if not rows:
        raise ValueError(f"{path.name}: CSV contains no rows")
    return rows


def load_domain_qa_spec() -> tuple[DomainQASpec, str]:
    resource = files("finance_agent_core.evaluation.suites").joinpath(DOMAIN_QA_SPEC_RESOURCE)
    raw = resource.read_bytes()
    return DomainQASpec.model_validate_json(raw), _sha256_bytes(raw)


def _path_contract(
    path: DomainQAEvaluationPath,
) -> tuple[
    list[InteractionIntent],
    list[BackendStatus],
    bool,
    DomainQAGoldLevel,
    str,
]:
    contracts = {
        DomainQAEvaluationPath.SEARCH: (
            [InteractionIntent.SEARCH],
            [BackendStatus.SUCCESS, BackendStatus.NOT_FOUND],
            False,
            DomainQAGoldLevel.ORACLE_PENDING,
            "structured_search",
        ),
        DomainQAEvaluationPath.CLARIFY: (
            [InteractionIntent.CLARIFY],
            [BackendStatus.CLARIFICATION],
            True,
            DomainQAGoldLevel.ROUTE_ONLY,
            "ambiguity_resolution",
        ),
        DomainQAEvaluationPath.UNSUPPORTED: (
            [InteractionIntent.UNSUPPORTED],
            [BackendStatus.UNSUPPORTED],
            True,
            DomainQAGoldLevel.ROUTE_ONLY,
            "unsupported_and_recommendation_safety",
        ),
        DomainQAEvaluationPath.DOCUMENT_RAG: (
            [InteractionIntent.EXPLAIN],
            [BackendStatus.CLARIFICATION, BackendStatus.UNSUPPORTED],
            True,
            DomainQAGoldLevel.DEPENDENCY_PENDING,
            "document_grounded_explanation",
        ),
        DomainQAEvaluationPath.EXTERNAL_POLICY: (
            [InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED],
            [BackendStatus.CLARIFICATION, BackendStatus.UNSUPPORTED],
            True,
            DomainQAGoldLevel.DEPENDENCY_PENDING,
            "external_policy_boundary",
        ),
        DomainQAEvaluationPath.EXTERNAL_DATA: (
            [InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED],
            [BackendStatus.CLARIFICATION, BackendStatus.UNSUPPORTED],
            True,
            DomainQAGoldLevel.DEPENDENCY_PENDING,
            "external_data_boundary",
        ),
    }
    return contracts[path]


def _expected_families(
    case_id: str,
    source_product_group: str,
    spec: DomainQASpec,
) -> list[ProductFamily] | None:
    if case_id in spec.family_overrides:
        return spec.family_overrides[case_id]
    mapping = {
        "국내채권": [ProductFamily.BOND],
        "해외 ETF·ETN": [ProductFamily.OVERSEAS_ETP],
        "국내 ETF·ETN": [ProductFamily.DOMESTIC_ETP],
        "공모펀드": [ProductFamily.FUND],
    }
    return mapping.get(source_product_group)


def _require_counts(
    label: str,
    observed: Counter[str],
    expected: dict[str, int],
) -> None:
    if dict(observed) != expected:
        raise ValueError(
            f"domain QA {label} counts differ: expected {expected}, got {dict(observed)}"
        )


def build_domain_qa_suite(
    spec: DomainQASpec,
    questions_csv: Path,
    review_csv: Path,
) -> DomainQASuite:
    if sha256_file(questions_csv) != spec.source_questions_sha256:
        raise ValueError("domain QA source question CSV SHA-256 differs")
    if sha256_file(review_csv) != spec.review_csv_sha256:
        raise ValueError("domain QA review CSV SHA-256 differs")
    question_rows = _read_csv(questions_csv, _QUESTION_HEADERS)
    review_rows = _read_csv(review_csv, _REVIEW_HEADERS)
    if len(question_rows) != spec.case_count or len(review_rows) != spec.case_count:
        raise ValueError("domain QA CSV row count differs from the spec")
    expected_ids = [f"Q{index:03d}" for index in range(1, spec.case_count + 1)]
    if [row["번호"] for row in question_rows] != expected_ids:
        raise ValueError("domain QA source IDs must be ordered and contiguous")
    if [row["번호"] for row in review_rows] != expected_ids:
        raise ValueError("domain QA review IDs must be ordered and contiguous")

    cases: list[DomainQACase] = []
    for source, review in zip(question_rows, review_rows, strict=True):
        if source["사용자 질문"] != review["사용자 질문"]:
            raise ValueError(f"{source['번호']}: source and review question differ")
        if source["상품군"] != review["상품군"]:
            raise ValueError(f"{source['번호']}: source and review product group differ")
        if source["기대하는 처리"] != review["원래 기대 처리"]:
            raise ValueError(f"{source['번호']}: source and review original action differ")
        path = DomainQAEvaluationPath(review["평가 경로"])
        intents, statuses, require_control, gold_level, capability = _path_contract(path)
        cases.append(
            DomainQACase(
                id=source["번호"],
                source_product_group=source["상품군"],
                question=source["사용자 질문"],
                scenario=source["질문을 하는 상황 또는 목적"],
                financial_note=source["금융적으로 주의할 점 또는 메모"],
                original_expected_action=source["기대하는 처리"],
                review_class=DomainQAReviewClass(review["검토 분류"]),
                recommended_action=review["현재 권장 처리"],
                data_support=review["현재 데이터 지원"],
                evaluation_path=path,
                severity=DomainQASeverity(review["심각도"]),
                rationale=review["검토 근거"],
                next_action=review["후속 조치"],
                behavioral_test_type=BehavioralTestType.MINIMUM_FUNCTIONALITY,
                parent_case_id=None,
                capability=capability,
                expected_interaction_intents=intents,
                allowed_backend_statuses=statuses,
                expected_product_families=_expected_families(
                    source["번호"],
                    source["상품군"],
                    spec,
                ),
                require_control=require_control,
                gold_level=gold_level,
            )
        )

    count_contract = spec.expected_counts
    _require_counts(
        "product group",
        Counter(case.source_product_group for case in cases),
        count_contract.product_group,
    )
    _require_counts(
        "review class",
        Counter(case.review_class.value for case in cases),
        count_contract.review_class,
    )
    _require_counts(
        "recommended action",
        Counter(case.recommended_action for case in cases),
        count_contract.recommended_action,
    )
    _require_counts(
        "data support",
        Counter(case.data_support for case in cases),
        count_contract.data_support,
    )
    _require_counts(
        "evaluation path",
        Counter(case.evaluation_path.value for case in cases),
        count_contract.evaluation_path,
    )
    _require_counts(
        "severity",
        Counter(case.severity.value for case in cases),
        count_contract.severity,
    )
    return DomainQASuite(
        schema_version=spec.schema_version,
        suite_id=spec.suite_id,
        suite_version=spec.suite_version,
        status=spec.status,
        source_questions_sha256=spec.source_questions_sha256,
        review_csv_sha256=spec.review_csv_sha256,
        cases=cases,
    )


def load_domain_qa_suite(
    questions_csv: Path,
    review_csv: Path,
) -> LoadedDomainQASuite:
    spec, spec_sha256 = load_domain_qa_spec()
    return LoadedDomainQASuite(
        spec=spec,
        spec_sha256=spec_sha256,
        suite=build_domain_qa_suite(spec, questions_csv, review_csv),
    )


def verify_domain_qa_databases(
    spec: DomainQASpec,
    database_paths: Mapping[ProductFamily | str, str | Path],
) -> dict[str, str]:
    normalized = {ProductFamily(family): Path(path) for family, path in database_paths.items()}
    if set(normalized) != set(ProductFamily):
        raise ValueError("database paths must configure all four product families")
    observed: dict[str, str] = {}
    for family in ProductFamily:
        path = normalized[family]
        database_sha256 = sha256_file(path)
        expected = spec.data[family]
        if database_sha256 != expected.database_sha256:
            raise ValueError(
                f"{family.value} database SHA-256 differs: "
                f"expected {expected.database_sha256}, got {database_sha256}"
            )
        manifest_path = path.with_suffix(f"{path.suffix}.manifest.json")
        manifest_sha256 = sha256_file(manifest_path)
        if manifest_sha256 != expected.manifest_sha256:
            raise ValueError(
                f"{family.value} manifest SHA-256 differs: "
                f"expected {expected.manifest_sha256}, got {manifest_sha256}"
            )
        observed[family.value] = database_sha256
    return observed


def _evidence_count(response: BackendAgentResponse) -> int:
    return (
        len(response.products)
        + len(response.comparisons)
        + len(response.aggregates)
        + len(response.documents)
    )


def evaluate_domain_qa_case(
    case: DomainQACase,
    adapter: AnswerAdapterResult,
    *,
    latency_ms: float,
) -> DomainQACaseResult:
    response = adapter.response
    evidence_count = _evidence_count(response)
    expected_families = case.expected_product_families
    family_exact = (
        True if expected_families is None else response.product_families == expected_families
    )
    is_executed = response.status in {BackendStatus.SUCCESS, BackendStatus.NOT_FOUND}
    control_boundary = (
        not is_executed
        and response.query_plan is None
        and response.candidate_count is None
        and evidence_count == 0
        and not response.citations
        and not response.as_of_dates
    )
    if response.status is BackendStatus.SUCCESS:
        execution_evidence = (
            response.query_plan is not None
            and response.candidate_count is not None
            and evidence_count > 0
            and bool(response.citations)
            and bool(response.as_of_dates)
        )
    elif response.status is BackendStatus.NOT_FOUND:
        execution_evidence = (
            response.query_plan is not None
            and response.candidate_count == 0
            and evidence_count == 0
            and not response.citations
        )
    else:
        execution_evidence = False

    answer_casefold = response.answer.casefold()
    checks = {
        "transport.http_status_200": adapter.http_status_code == 200,
        "route.backend_status_allowed": response.status in case.allowed_backend_statuses,
        "route.interaction_intent_allowed": (response.intent in case.expected_interaction_intents),
        "route.product_families_exact": family_exact,
        "plan.execution_boundary": (
            control_boundary if case.require_control else execution_evidence
        ),
        "retrieval.evidence_shape_valid": (
            evidence_count == 0 if case.require_control else execution_evidence
        ),
        "evidence.citations_and_dates_valid": (
            not response.citations and not response.as_of_dates
            if case.require_control
            else execution_evidence
        ),
        "answer.forbidden_fragments_absent": not any(
            fragment.casefold() in answer_casefold
            for fragment in _GLOBAL_FORBIDDEN_ANSWER_FRAGMENTS
        ),
        "answer.fallback_state_consistent": response.fallback_used
        == (response.answer_mode is BackendAnswerMode.DETERMINISTIC_FALLBACK),
        "safety.control_not_executed": (control_boundary if case.require_control else True),
        "contract.request_id_preserved": response.request_id == case.id,
        "contract.no_backend_error": response.error is None,
    }
    violations = [name for name, passed in checks.items() if not passed]

    def stage(prefixes: tuple[str, ...]) -> bool:
        selected = [passed for name, passed in checks.items() if name.startswith(prefixes)]
        return bool(selected) and all(selected)

    route_passed = stage(("route.",))
    safety_passed = stage(("transport.", "safety.", "contract."))
    evidence_passed = stage(("plan.", "retrieval.", "evidence."))
    answer_passed = stage(("answer.",))
    plan_sha256 = (
        None
        if response.query_plan is None
        else _canonical_sha256(response.query_plan.model_dump(mode="json"))
    )
    return DomainQACaseResult(
        id=case.id,
        source_product_group=case.source_product_group,
        evaluation_path=case.evaluation_path,
        severity=case.severity,
        capability=case.capability,
        behavioral_test_type=case.behavioral_test_type,
        gold_level=case.gold_level,
        expected_interaction_intents=case.expected_interaction_intents,
        allowed_backend_statuses=case.allowed_backend_statuses,
        expected_product_families=expected_families,
        actual_http_status=adapter.http_status_code,
        actual_backend_status=response.status,
        actual_interaction_intent=response.intent,
        actual_product_families=response.product_families,
        answer_mode=response.answer_mode,
        fallback_used=response.fallback_used,
        candidate_count=response.candidate_count,
        evidence_count=evidence_count,
        citation_count=len(response.citations),
        latency_ms=latency_ms,
        plan_sha256=plan_sha256,
        checks=checks,
        violations=violations,
        route_passed=route_passed,
        safety_passed=safety_passed,
        evidence_passed=evidence_passed,
        answer_passed=answer_passed,
        strict_passed=all(checks.values()),
    )


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


def _group_accuracy(
    cases: Sequence[DomainQACase],
    results: Sequence[DomainQACaseResult],
    attribute: Literal[
        "source_product_group",
        "evaluation_path",
        "severity",
        "capability",
        "behavioral_test_type",
    ],
) -> dict[str, dict[str, int | float]]:
    totals: Counter[str] = Counter()
    passed: Counter[str] = Counter()
    for case, result in zip(cases, results, strict=True):
        value = getattr(case, attribute)
        key = value.value if isinstance(value, StrEnum) else value
        totals[key] += 1
        if result.strict_passed:
            passed[key] += 1
    return {
        key: {
            "total": total,
            "passed": passed[key],
            "accuracy": round(passed[key] / total, 6),
        }
        for key, total in sorted(totals.items())
    }


def build_domain_qa_report(
    *,
    loaded_suite: LoadedDomainQASuite,
    database_sha256_by_family: dict[str, str],
    results: list[DomainQACaseResult],
    generated_at_utc: str,
    report_id: str,
) -> DomainQAReport:
    cases = loaded_suite.suite.cases
    if len(results) != len(cases):
        raise ValueError("one domain QA result is required for every case")
    total = len(results)
    passed = sum(result.strict_passed for result in results)
    route_passed = sum(result.route_passed for result in results)
    safety_passed = sum(result.safety_passed for result in results)
    evidence_passed = sum(result.evidence_passed for result in results)
    answer_passed = sum(result.answer_passed for result in results)
    latencies = [result.latency_ms for result in results]
    failure_taxonomy = Counter(violation for result in results for violation in result.violations)
    summary = DomainQASummary(
        total=total,
        passed=passed,
        strict_accuracy=round(passed / total, 6),
        route_passed=route_passed,
        route_pass_rate=round(route_passed / total, 6),
        safety_passed=safety_passed,
        safety_pass_rate=round(safety_passed / total, 6),
        evidence_passed=evidence_passed,
        evidence_pass_rate=round(evidence_passed / total, 6),
        answer_passed=answer_passed,
        answer_pass_rate=round(answer_passed / total, 6),
        dependency_pending=sum(
            case.gold_level is DomainQAGoldLevel.DEPENDENCY_PENDING for case in cases
        ),
        oracle_gold_pending=sum(
            case.gold_level is DomainQAGoldLevel.ORACLE_PENDING for case in cases
        ),
        actual_status_counts=dict(
            sorted(Counter(result.actual_backend_status.value for result in results).items())
        ),
        test_type_counts=dict(
            sorted(Counter(case.behavioral_test_type.value for case in cases).items())
        ),
        by_evaluation_path=_group_accuracy(cases, results, "evaluation_path"),
        by_product_group=_group_accuracy(cases, results, "source_product_group"),
        by_severity=_group_accuracy(cases, results, "severity"),
        by_capability=_group_accuracy(cases, results, "capability"),
        failure_taxonomy=dict(sorted(failure_taxonomy.items())),
        latency_ms={
            "min": round(min(latencies), 3),
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 3),
        },
        perfect=passed == total,
    )
    return DomainQAReport(
        report_id=report_id,
        generated_at_utc=generated_at_utc,
        profile="deterministic_current",
        suite_id=loaded_suite.suite.suite_id,
        suite_version=loaded_suite.suite.suite_version,
        suite_spec_sha256=loaded_suite.spec_sha256,
        source_questions_sha256=loaded_suite.suite.source_questions_sha256,
        review_csv_sha256=loaded_suite.suite.review_csv_sha256,
        database_sha256_by_family=database_sha256_by_family,
        summary=summary,
        cases=results,
        interpretation_limits=[
            "금융 도메인 담당자가 작성하고 AI 담당자가 검토한 개발 세트이며 독립 blind가 아니다.",
            "현재 40문항은 CheckList minimum-functionality test이며 invariance와 "
            "directional-expectation 증강은 아직 없다.",
            "SEARCH 한 문항의 gold QueryPlan·Oracle denotation은 아직 확정되지 않아 "
            "정확한 상품 집합과 순위를 채점하지 않는다.",
            "문서 RAG·외부 정책·외부 데이터 13문항은 승인된 dependency가 없어 "
            "현재는 안전한 control 경계만 평가한다.",
            "이번 최초 관측은 Router와 Backend 경로의 공백을 측정하며 발견된 실패를 "
            "수정한 사후 회귀와 분리해야 한다.",
        ],
    )


class DomainQARunner:
    def __init__(
        self,
        *,
        loaded_suite: LoadedDomainQASuite,
        service: RoutedAnswerService,
        database_sha256_by_family: dict[str, str],
        generated_at_utc: str,
        report_id: str,
    ) -> None:
        self.loaded_suite = loaded_suite
        self.service = service
        self.database_sha256_by_family = database_sha256_by_family
        self.generated_at_utc = generated_at_utc
        self.report_id = report_id

    def run(self) -> DomainQAReport:
        results: list[DomainQACaseResult] = []
        for case in self.loaded_suite.suite.cases:
            request = BackendAgentRequest(request_id=case.id, question=case.question)
            started = time.perf_counter()
            adapter = execute_answer_request(self.service, request)
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            results.append(
                evaluate_domain_qa_case(
                    case,
                    adapter,
                    latency_ms=latency_ms,
                )
            )
        return build_domain_qa_report(
            loaded_suite=self.loaded_suite,
            database_sha256_by_family=self.database_sha256_by_family,
            results=results,
            generated_at_utc=self.generated_at_utc,
            report_id=self.report_id,
        )
