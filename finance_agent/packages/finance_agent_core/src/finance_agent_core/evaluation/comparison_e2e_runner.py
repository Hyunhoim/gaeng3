from __future__ import annotations

import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent import (
    FundComparisonDraft,
    FundProductResolver,
    compile_fund_comparison_query_plan,
)
from finance_agent_core.agent.fund_comparison_parser import FundComparisonDraftProvider
from finance_agent_core.answering import (
    GroundedAnswerDraft,
    GroundedAnswerProvider,
    build_grounded_answer_context,
    compose_grounded_answer,
)
from finance_agent_core.evaluation.answer_runner import _evidence_citations_present
from finance_agent_core.evaluation.comparison_parser_runner import (
    ExpectedResolutionStatus,
    FundComparisonParserCase,
    LoadedFundComparisonParserSuite,
    fund_comparison_plan_contract_exact,
)
from finance_agent_core.evaluation.models import (
    EvaluationSplit,
    ExpectedBlocker,
    ExpectedDisposition,
)
from finance_agent_core.execution import (
    FundComparison,
    PlanExecutionBlockedError,
    ResultVerifier,
    SQLiteOracle,
    build_fund_comparison,
    build_product_evidence,
    fund_comparison_product_ids,
    render_blocked_plan,
    require_internal_evaluation_comparison,
)
from finance_agent_core.storage import connect_read_only, load_all_records, load_manifest

type E2ECompositionMode = Literal[
    "llm_grounded",
    "deterministic",
    "deterministic_fallback",
    "blocked",
    "error",
]
type ComparisonStatus = Literal[
    "numeric_delta",
    "value_only",
    "currency_mismatch",
    "unavailable",
    "incomplete",
]


class ComparisonE2EEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FundComparisonE2EExpectation(ComparisonE2EEvaluationModel):
    case_id: str = Field(min_length=1, max_length=128)
    field_statuses: dict[str, ComparisonStatus]
    deltas: dict[str, str | None]
    cell_value_fingerprints: dict[str, list[str]]
    evidence_fingerprints: dict[str, list[str]]

    @model_validator(mode="after")
    def validate_fields_and_deltas(self) -> FundComparisonE2EExpectation:
        expected_fields = list(self.field_statuses)
        if any(
            list(mapping) != expected_fields
            for mapping in (
                self.deltas,
                self.cell_value_fingerprints,
                self.evidence_fingerprints,
            )
        ):
            raise ValueError("all frozen comparison maps must use the same field order")
        for field_name, status in self.field_statuses.items():
            delta = self.deltas[field_name]
            if (status == "numeric_delta") != (delta is not None):
                raise ValueError("only numeric_delta fields may declare a frozen delta")
            for mapping in (
                self.cell_value_fingerprints,
                self.evidence_fingerprints,
            ):
                fingerprints = mapping[field_name]
                if len(fingerprints) != 2:
                    raise ValueError("each comparison field requires two frozen fingerprints")
                if any(
                    len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)
                    for value in fingerprints
                ):
                    raise ValueError("comparison fingerprints must be lowercase SHA-256")
        return self


class FundComparisonE2ESuite(ComparisonE2EEvaluationModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["fund-compare-e2e-core-24"]
    suite_version: Literal["1.0"]
    source_suite_id: Literal["fund-compare-parser-core-24"]
    source_suite_version: Literal["1.0"]
    source_suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset: Literal["fund"]
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: list[FundComparisonE2EExpectation] = Field(min_length=16, max_length=16)

    @model_validator(mode="after")
    def validate_unique_cases(self) -> FundComparisonE2ESuite:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("comparison E2E expectation case IDs must be unique")
        return self


@dataclass(frozen=True)
class LoadedFundComparisonE2ESuite:
    suite: FundComparisonE2ESuite
    suite_sha256: str
    parser_suite: LoadedFundComparisonParserSuite

    @property
    def expectations(self) -> dict[str, FundComparisonE2EExpectation]:
        return {case.case_id: case for case in self.suite.cases}


class FundComparisonE2ECaseResult(ComparisonE2EEvaluationModel):
    case_id: str
    split: EvaluationSplit
    category: str
    question: str
    disposition: ExpectedDisposition
    passed: bool
    mode: E2ECompositionMode
    parser_generation_latency_ms: float = Field(ge=0)
    answer_generation_latency_ms: float = Field(ge=0)
    total_latency_ms: float = Field(ge=0)
    checks: dict[str, bool]
    resolution_statuses: list[ExpectedResolutionStatus]
    resolved_product_ids: list[str]
    comparison_fields: list[str]
    found_product_ids: list[str]
    missing_product_ids: list[str]
    field_statuses: dict[str, str]
    deltas: dict[str, str | None]
    cell_value_fingerprints: dict[str, list[str]]
    evidence_fingerprints: dict[str, list[str]]
    comparison_draft: FundComparisonDraft | None
    answer_draft: GroundedAnswerDraft | None
    answer_verifier_checks: dict[str, bool]
    violations: list[str]
    answer: str
    error: str | None


class FundComparisonE2ESummary(ComparisonE2EEvaluationModel):
    total: int
    passed: int
    strict_accuracy: float
    executable_cases: int
    blocked_cases: int
    parser_calls: int
    answer_generation_attempts: int
    llm_grounded_cases: int
    fallback_cases: int
    fallback_rate: float
    draft_target_exact_rate: float
    draft_field_exact_rate: float
    mention_grounding_rate: float
    resolution_exact_rate: float
    plan_exact_rate: float
    oracle_exact_rate: float | None
    field_status_accuracy: float | None
    numeric_delta_accuracy: float | None
    cell_value_accuracy: float | None
    evidence_provenance_accuracy: float | None
    safety_block_rate: float | None
    blocked_answer_safety_rate: float | None
    blocked_answer_suppression_rate: float | None
    answer_verifier_pass_rate: float | None
    deterministic_core_rate: float | None
    evidence_citation_rate: float | None
    source_date_coverage_rate: float | None
    parser_latency_ms_p50: float
    parser_latency_ms_p95: float
    parser_latency_ms_max: float
    answer_latency_ms_p50: float
    answer_latency_ms_p95: float
    answer_latency_ms_max: float
    total_latency_ms_p50: float
    total_latency_ms_p95: float
    total_latency_ms_max: float
    failures: list[str]
    by_split: dict[str, dict[str, float | int]]
    by_category: dict[str, dict[str, float | int]]


class FundComparisonE2EReport(ComparisonE2EEvaluationModel):
    suite_id: str
    suite_version: str
    suite_sha256: str
    source_suite_id: str
    source_suite_version: str
    source_suite_sha256: str
    database_sha256: str
    manifest_sha256: str
    provider: str
    parser_model: str | None
    answer_model: str | None
    split: Literal["development", "holdout", "all"]
    workers: int
    isolation: Literal["natural_question_to_verified_grounded_answer"] = (
        "natural_question_to_verified_grounded_answer"
    )
    summary: FundComparisonE2ESummary
    results: list[FundComparisonE2ECaseResult]


def load_fund_comparison_e2e_suite() -> LoadedFundComparisonE2ESuite:
    from finance_agent_core.evaluation.comparison_parser_runner import (
        load_fund_comparison_parser_suite,
    )

    parser_suite = load_fund_comparison_parser_suite()
    resource = files("finance_agent_core.evaluation.suites").joinpath(
        "fund_compare_e2e_core_24.json"
    )
    raw = resource.read_bytes()
    suite = FundComparisonE2ESuite.model_validate(json.loads(raw))
    if (
        suite.source_suite_id != parser_suite.suite.suite_id
        or suite.source_suite_version != parser_suite.suite.suite_version
        or suite.source_suite_sha256 != parser_suite.suite_sha256
    ):
        raise ValueError("comparison E2E source parser suite contract differs")
    if (
        suite.database_sha256 != parser_suite.suite.database_sha256
        or suite.manifest_sha256 != parser_suite.suite.manifest_sha256
    ):
        raise ValueError("comparison E2E data hashes differ from the parser suite")
    executable = {
        case.id: case
        for case in parser_suite.suite.cases
        if case.expected.disposition is ExpectedDisposition.EXECUTE
    }
    expectations = {case.case_id: case for case in suite.cases}
    if set(expectations) != set(executable):
        raise ValueError("comparison E2E expectations must cover every executable case")
    for case_id, expectation in expectations.items():
        if list(expectation.field_statuses) != executable[case_id].expected.comparison_fields:
            raise ValueError(f"comparison E2E fields differ from parser case {case_id}")
    return LoadedFundComparisonE2ESuite(
        suite=suite,
        suite_sha256=hashlib.sha256(raw).hexdigest(),
        parser_suite=parser_suite,
    )


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _decimal_string(value) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _payload_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _comparison_scalar(value: object | None) -> str | int | bool | None:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported comparison value: {value!r}")


def comparison_cell_value_fingerprints(
    comparison: FundComparison,
) -> dict[str, list[str]]:
    return {
        field.canonical_field: [
            _payload_sha256(
                {
                    "canonical_field": field.canonical_field,
                    "target_index": cell.target_index,
                    "product_id": cell.product_id,
                    "cell_value": _comparison_scalar(cell.value),
                }
            )
            for cell in field.cells
        ]
        for field in comparison.fields
    }


def comparison_evidence_fingerprints(
    comparison: FundComparison,
) -> dict[str, list[str]]:
    return {
        field.canonical_field: [
            _payload_sha256(
                {
                    "canonical_field": field.canonical_field,
                    "target_index": cell.target_index,
                    "product_id": cell.product_id,
                    "evidence": (
                        None if cell.evidence is None else cell.evidence.model_dump(mode="json")
                    ),
                }
            )
            for cell in field.cells
        ]
        for field in comparison.fields
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _group_metrics(
    results: list[FundComparisonE2ECaseResult],
    attribute: Literal["split", "category"],
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[FundComparisonE2ECaseResult]] = {}
    for result in results:
        value = getattr(result, attribute)
        key = value.value if isinstance(value, EvaluationSplit) else value
        grouped.setdefault(key, []).append(result)
    return {
        key: {
            "total": len(items),
            "passed": sum(item.passed for item in items),
            "accuracy": _rate(sum(item.passed for item in items), len(items)),
        }
        for key, items in sorted(grouped.items())
    }


class FundComparisonE2EEvaluationRunner:
    """Evaluate the full natural-question-to-grounded-answer comparison path."""

    def __init__(
        self,
        database_path: str | Path,
        draft_provider: FundComparisonDraftProvider,
        answer_provider: GroundedAnswerProvider,
        comparison_expectations: dict[str, FundComparisonE2EExpectation],
    ) -> None:
        allowed = {"expected", "local_test"}
        if draft_provider.provider_name not in allowed:
            raise ValueError("comparison E2E draft provider requires expected or local_test")
        if answer_provider.provider_name not in allowed:
            raise ValueError("comparison E2E answer provider requires expected or local_test")
        if draft_provider.provider_name != answer_provider.provider_name:
            raise ValueError("comparison E2E providers must use the same provider mode")
        if draft_provider.model_name != answer_provider.model_name:
            raise ValueError("comparison E2E providers must use the same model")

        self.database_path = Path(database_path)
        self.draft_provider = draft_provider
        self.answer_provider = answer_provider
        self.comparison_expectations = comparison_expectations
        self.oracle = SQLiteOracle(self.database_path)
        self.verifier = ResultVerifier()
        with connect_read_only(self.database_path) as connection:
            manifest = load_manifest(connection)
            self.universe = load_all_records(connection)
        if manifest.dataset != "fund":
            raise ValueError("comparison E2E evaluation requires a fund database")
        self.resolver = FundProductResolver(self.universe)

    def run_case(self, case: FundComparisonParserCase) -> FundComparisonE2ECaseResult:
        started = time.perf_counter()
        parser_completed_at = started
        parser_latency_ms = 0.0
        answer_latency_ms = 0.0
        draft: FundComparisonDraft | None = None
        answer_draft: GroundedAnswerDraft | None = None
        resolution_statuses: list[ExpectedResolutionStatus] = []
        resolved_product_ids: list[str] = []
        comparison_fields: list[str] = []
        found_product_ids: list[str] = []
        missing_product_ids: list[str] = []
        field_statuses: dict[str, str] = {}
        deltas: dict[str, str | None] = {}
        cell_value_fingerprints: dict[str, list[str]] = {}
        evidence_fingerprints: dict[str, list[str]] = {}
        answer_verifier_checks: dict[str, bool] = {}
        violations: list[str] = []
        answer = ""
        checks: dict[str, bool] = {"draft_schema_valid": False}

        try:
            draft = self.draft_provider.generate_comparison_draft(case.question, case.id)
            parser_completed_at = time.perf_counter()
            parser_latency_ms = round((parser_completed_at - started) * 1000, 3)
            checks.update(
                {
                    "draft_schema_valid": True,
                    "draft_targets_exact": (
                        draft.target_mentions == case.expected.draft.target_mentions
                    ),
                    "draft_fields_exact": (
                        draft.comparison_fields == case.expected.draft.comparison_fields
                    ),
                }
            )
            compiled = compile_fund_comparison_query_plan(
                question=case.question,
                question_id=case.id,
                draft=draft,
                resolver=self.resolver,
            )
            resolution_statuses = [resolution.status for resolution in compiled.resolutions]
            resolved_product_ids = list(compiled.resolved_product_ids)
            comparison_fields = list(compiled.comparison_fields)
            checks.update(
                {
                    "mentions_grounded": all(compiled.mentions_grounded),
                    "resolution_statuses_exact": (
                        resolution_statuses == case.expected.resolution_statuses
                    ),
                    "resolved_product_ids_exact": (
                        resolved_product_ids == case.expected.resolved_product_ids
                    ),
                    "comparison_fields_exact": (
                        comparison_fields == case.expected.comparison_fields
                    ),
                    "question_targets_complete": compiled.targets_complete,
                    "target_roles_unambiguous": compiled.target_roles_unambiguous,
                    "plan_exact": fund_comparison_plan_contract_exact(case, compiled),
                }
            )

            if case.expected.disposition is ExpectedDisposition.BLOCK:
                try:
                    require_internal_evaluation_comparison(compiled.plan)
                except PlanExecutionBlockedError:
                    checks["execution_blocked"] = True
                else:
                    checks["execution_blocked"] = False
                if case.expected.blocker is ExpectedBlocker.AMBIGUITY:
                    checks["blocker_shape_exact"] = (
                        bool(compiled.plan.ambiguities) and not compiled.plan.unsupported_conditions
                    )
                else:
                    checks["blocker_shape_exact"] = (
                        bool(compiled.plan.unsupported_conditions) and not compiled.plan.ambiguities
                    )
                checks["answer_generation_suppressed"] = True
                answer = render_blocked_plan(compiled.plan, "fund")
                checks["safe_blocked_answer"] = bool(answer.strip())
                finished = time.perf_counter()
                return FundComparisonE2ECaseResult(
                    case_id=case.id,
                    split=case.split,
                    category=case.category,
                    question=case.question,
                    disposition=case.expected.disposition,
                    passed=all(checks.values()),
                    mode="blocked",
                    parser_generation_latency_ms=parser_latency_ms,
                    answer_generation_latency_ms=0,
                    total_latency_ms=round((finished - started) * 1000, 3),
                    checks=checks,
                    resolution_statuses=resolution_statuses,
                    resolved_product_ids=resolved_product_ids,
                    comparison_fields=comparison_fields,
                    found_product_ids=[],
                    missing_product_ids=[],
                    field_statuses={},
                    deltas={},
                    cell_value_fingerprints={},
                    evidence_fingerprints={},
                    comparison_draft=draft,
                    answer_draft=None,
                    answer_verifier_checks={},
                    violations=[],
                    answer=answer,
                    error=None,
                )

            require_internal_evaluation_comparison(compiled.plan)
            checks["execution_allowed"] = True
            checks["identity_constraint_exact"] = (
                fund_comparison_product_ids(compiled.plan) == case.expected.resolved_product_ids
            )
            executed = self.oracle.execute(compiled.plan)
            verified = self.verifier.verify(compiled.plan, executed, self.universe)
            products = build_product_evidence(compiled.plan, verified)
            comparison = build_fund_comparison(compiled.plan, verified, products)
            found_product_ids = list(comparison.found_product_ids)
            missing_product_ids = list(comparison.missing_product_ids)
            field_statuses = {field.canonical_field: field.status for field in comparison.fields}
            deltas = {
                field.canonical_field: _decimal_string(field.delta) for field in comparison.fields
            }
            cell_value_fingerprints = comparison_cell_value_fingerprints(comparison)
            evidence_fingerprints = comparison_evidence_fingerprints(comparison)
            expectation = self.comparison_expectations.get(case.id)
            if expectation is None:
                raise ValueError(f"missing comparison E2E expectation: {case.id}")
            checks.update(
                {
                    "result_verifier_passed": True,
                    "oracle_exact": (
                        found_product_ids == case.expected.resolved_product_ids
                        and not missing_product_ids
                        and comparison.verified.candidate_count == 2
                    ),
                    "comparison_fields_materialized": (
                        list(field_statuses) == case.expected.comparison_fields
                    ),
                    "field_statuses_exact": (field_statuses == expectation.field_statuses),
                    "numeric_deltas_exact": deltas == expectation.deltas,
                    "cell_values_exact": (
                        cell_value_fingerprints == expectation.cell_value_fingerprints
                    ),
                    "evidence_provenance_exact": (
                        evidence_fingerprints == expectation.evidence_fingerprints
                    ),
                }
            )
            context = build_grounded_answer_context(
                question=case.question,
                plan=compiled.plan,
                verified=comparison.verified,
                products=list(comparison.products),
            )
            composition = compose_grounded_answer(
                question=case.question,
                plan=compiled.plan,
                verified=comparison.verified,
                products=list(comparison.products),
                provider=self.answer_provider,
            )
            answer_latency_ms = composition.generation_latency_ms
            answer_draft = composition.draft
            answer_verifier_checks = composition.verification.checks
            violations = composition.verification.violations
            answer = composition.answer
            checks.update(
                {
                    "answer_mode_grounded": composition.mode == "llm_grounded",
                    "fallback_not_used": composition.mode != "deterministic_fallback",
                    "answer_verifier_passed": composition.verification.passed,
                    "deterministic_core_preserved": (
                        context.deterministic_answer in composition.answer
                    ),
                    "evidence_citations_present": (
                        _evidence_citations_present(
                            composition.answer,
                            context,
                            composition.draft,
                        )
                        and composition.verification.checks.get(
                            "compiled_evidence_citations_exact",
                            False,
                        )
                    ),
                    "source_date_covered": (
                        context.source_manifest.source_snapshot_date.isoformat()
                        in composition.answer
                    ),
                    "safe_answer": bool(composition.answer.strip()),
                }
            )
            finished = time.perf_counter()
            return FundComparisonE2ECaseResult(
                case_id=case.id,
                split=case.split,
                category=case.category,
                question=case.question,
                disposition=case.expected.disposition,
                passed=all(checks.values()),
                mode=composition.mode,
                parser_generation_latency_ms=parser_latency_ms,
                answer_generation_latency_ms=answer_latency_ms,
                total_latency_ms=round((finished - started) * 1000, 3),
                checks=checks,
                resolution_statuses=resolution_statuses,
                resolved_product_ids=resolved_product_ids,
                comparison_fields=comparison_fields,
                found_product_ids=found_product_ids,
                missing_product_ids=missing_product_ids,
                field_statuses=field_statuses,
                deltas=deltas,
                cell_value_fingerprints=cell_value_fingerprints,
                evidence_fingerprints=evidence_fingerprints,
                comparison_draft=draft,
                answer_draft=answer_draft,
                answer_verifier_checks=answer_verifier_checks,
                violations=violations,
                answer=answer,
                error=None,
            )
        except Exception as error:  # noqa: BLE001 - every case must become a result
            finished = time.perf_counter()
            if parser_completed_at == started:
                parser_completed_at = finished
                parser_latency_ms = round((parser_completed_at - started) * 1000, 3)
            return FundComparisonE2ECaseResult(
                case_id=case.id,
                split=case.split,
                category=case.category,
                question=case.question,
                disposition=case.expected.disposition,
                passed=False,
                mode="error",
                parser_generation_latency_ms=parser_latency_ms,
                answer_generation_latency_ms=answer_latency_ms,
                total_latency_ms=round((finished - started) * 1000, 3),
                checks=checks,
                resolution_statuses=resolution_statuses,
                resolved_product_ids=resolved_product_ids,
                comparison_fields=comparison_fields,
                found_product_ids=found_product_ids,
                missing_product_ids=missing_product_ids,
                field_statuses=field_statuses,
                deltas=deltas,
                cell_value_fingerprints=cell_value_fingerprints,
                evidence_fingerprints=evidence_fingerprints,
                comparison_draft=draft,
                answer_draft=answer_draft,
                answer_verifier_checks=answer_verifier_checks,
                violations=violations,
                answer=answer,
                error=f"{type(error).__name__}: {error}",
            )

    def run(
        self,
        cases: list[FundComparisonParserCase],
        workers: int,
    ) -> list[FundComparisonE2ECaseResult]:
        if workers < 1 or workers > 16:
            raise ValueError("workers must be in [1, 16]")
        if workers == 1:
            return [self.run_case(case) for case in cases]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(self.run_case, cases))


def build_fund_comparison_e2e_report(
    *,
    loaded: LoadedFundComparisonE2ESuite,
    draft_provider: FundComparisonDraftProvider,
    answer_provider: GroundedAnswerProvider,
    split: Literal["development", "holdout", "all"],
    workers: int,
    results: list[FundComparisonE2ECaseResult],
) -> FundComparisonE2EReport:
    executable = [result for result in results if result.disposition is ExpectedDisposition.EXECUTE]
    blocked = [result for result in results if result.disposition is ExpectedDisposition.BLOCK]
    parser_latencies = [result.parser_generation_latency_ms for result in results]
    answer_attempts = [
        result for result in executable if result.mode in {"llm_grounded", "deterministic_fallback"}
    ]
    answer_latencies = [result.answer_generation_latency_ms for result in answer_attempts]
    total_latencies = [result.total_latency_ms for result in results]
    fallback_cases = sum(result.mode == "deterministic_fallback" for result in executable)
    summary = FundComparisonE2ESummary(
        total=len(results),
        passed=sum(result.passed for result in results),
        strict_accuracy=_rate(sum(result.passed for result in results), len(results)),
        executable_cases=len(executable),
        blocked_cases=len(blocked),
        parser_calls=len(results),
        answer_generation_attempts=len(answer_attempts),
        llm_grounded_cases=sum(result.mode == "llm_grounded" for result in executable),
        fallback_cases=fallback_cases,
        fallback_rate=_rate(fallback_cases, len(answer_attempts)),
        draft_target_exact_rate=_rate(
            sum(result.checks.get("draft_targets_exact", False) for result in results),
            len(results),
        ),
        draft_field_exact_rate=_rate(
            sum(result.checks.get("draft_fields_exact", False) for result in results),
            len(results),
        ),
        mention_grounding_rate=_rate(
            sum(result.checks.get("mentions_grounded", False) for result in results),
            len(results),
        ),
        resolution_exact_rate=_rate(
            sum(
                result.checks.get("resolution_statuses_exact", False)
                and result.checks.get("resolved_product_ids_exact", False)
                for result in results
            ),
            len(results),
        ),
        plan_exact_rate=_rate(
            sum(result.checks.get("plan_exact", False) for result in results),
            len(results),
        ),
        oracle_exact_rate=(
            _rate(
                sum(result.checks.get("oracle_exact", False) for result in executable),
                len(executable),
            )
            if executable
            else None
        ),
        field_status_accuracy=(
            _rate(
                sum(result.checks.get("field_statuses_exact", False) for result in executable),
                len(executable),
            )
            if executable
            else None
        ),
        numeric_delta_accuracy=(
            _rate(
                sum(result.checks.get("numeric_deltas_exact", False) for result in executable),
                len(executable),
            )
            if executable
            else None
        ),
        cell_value_accuracy=(
            _rate(
                sum(result.checks.get("cell_values_exact", False) for result in executable),
                len(executable),
            )
            if executable
            else None
        ),
        evidence_provenance_accuracy=(
            _rate(
                sum(result.checks.get("evidence_provenance_exact", False) for result in executable),
                len(executable),
            )
            if executable
            else None
        ),
        safety_block_rate=(
            _rate(
                sum(result.checks.get("execution_blocked", False) for result in blocked),
                len(blocked),
            )
            if blocked
            else None
        ),
        blocked_answer_safety_rate=(
            _rate(
                sum(result.checks.get("safe_blocked_answer", False) for result in blocked),
                len(blocked),
            )
            if blocked
            else None
        ),
        blocked_answer_suppression_rate=(
            _rate(
                sum(result.checks.get("answer_generation_suppressed", False) for result in blocked),
                len(blocked),
            )
            if blocked
            else None
        ),
        answer_verifier_pass_rate=(
            _rate(
                sum(result.checks.get("answer_verifier_passed", False) for result in executable),
                len(executable),
            )
            if executable
            else None
        ),
        deterministic_core_rate=(
            _rate(
                sum(
                    result.checks.get("deterministic_core_preserved", False)
                    for result in executable
                ),
                len(executable),
            )
            if executable
            else None
        ),
        evidence_citation_rate=(
            _rate(
                sum(
                    result.checks.get("evidence_citations_present", False) for result in executable
                ),
                len(executable),
            )
            if executable
            else None
        ),
        source_date_coverage_rate=(
            _rate(
                sum(result.checks.get("source_date_covered", False) for result in executable),
                len(executable),
            )
            if executable
            else None
        ),
        parser_latency_ms_p50=_percentile(parser_latencies, 0.50),
        parser_latency_ms_p95=_percentile(parser_latencies, 0.95),
        parser_latency_ms_max=round(max(parser_latencies, default=0.0), 3),
        answer_latency_ms_p50=_percentile(answer_latencies, 0.50),
        answer_latency_ms_p95=_percentile(answer_latencies, 0.95),
        answer_latency_ms_max=round(max(answer_latencies, default=0.0), 3),
        total_latency_ms_p50=_percentile(total_latencies, 0.50),
        total_latency_ms_p95=_percentile(total_latencies, 0.95),
        total_latency_ms_max=round(max(total_latencies, default=0.0), 3),
        failures=[result.case_id for result in results if not result.passed],
        by_split=_group_metrics(results, "split"),
        by_category=_group_metrics(results, "category"),
    )
    return FundComparisonE2EReport(
        suite_id=loaded.suite.suite_id,
        suite_version=loaded.suite.suite_version,
        suite_sha256=loaded.suite_sha256,
        database_sha256=loaded.suite.database_sha256,
        manifest_sha256=loaded.suite.manifest_sha256,
        source_suite_id=loaded.suite.source_suite_id,
        source_suite_version=loaded.suite.source_suite_version,
        source_suite_sha256=loaded.suite.source_suite_sha256,
        provider=draft_provider.provider_name,
        parser_model=draft_provider.model_name,
        answer_model=answer_provider.model_name,
        split=split,
        workers=workers,
        summary=summary,
        results=results,
    )
