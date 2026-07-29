from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_agent_core.answering import (
    GroundedAnswerDraft,
    GroundedAnswerProvider,
    build_grounded_answer_context,
    compose_grounded_answer,
)
from finance_agent_core.domain import NormalizedProductRecord
from finance_agent_core.evaluation.models import (
    EvaluationCase,
    EvaluationSplit,
    ExpectedDisposition,
)
from finance_agent_core.execution import (
    PlanExecutionBlockedError,
    ResultVerifier,
    SQLiteOracle,
    build_product_evidence,
    render_blocked_plan,
    require_executable_search,
)
from finance_agent_core.storage import connect_read_only, load_all_records, load_manifest


class AnswerEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnswerCaseResult(AnswerEvaluationModel):
    case_id: str
    split: EvaluationSplit
    category: str
    question: str
    disposition: ExpectedDisposition
    passed: bool
    mode: Literal[
        "llm_grounded",
        "deterministic",
        "deterministic_fallback",
        "blocked",
        "error",
    ]
    generation_latency_ms: float = Field(ge=0)
    total_latency_ms: float = Field(ge=0)
    candidate_count: int | None
    product_ids: list[str]
    checks: dict[str, bool]
    violations: list[str]
    draft: GroundedAnswerDraft | None
    answer: str
    error: str | None


class AnswerEvaluationSummary(AnswerEvaluationModel):
    total: int
    passed: int
    strict_accuracy: float
    executable_cases: int
    blocked_cases: int
    llm_grounded_cases: int
    deterministic_empty_cases: int
    fallback_cases: int
    safe_answer_rate: float
    product_order_accuracy: float | None
    evidence_reference_accuracy: float | None
    field_evidence_citation_rate: float | None
    numeric_fidelity_rate: float | None
    warning_coverage_rate: float | None
    source_date_coverage_rate: float | None
    unsupported_claim_case_rate: float | None
    generation_latency_ms_p50: float
    generation_latency_ms_p95: float
    generation_latency_ms_max: float
    failures: list[str]
    by_split: dict[str, dict[str, float | int]]
    by_category: dict[str, dict[str, float | int]]


class AnswerEvaluationReport(AnswerEvaluationModel):
    suite_id: str
    suite_version: str
    suite_sha256: str
    database_sha256: str
    manifest_sha256: str
    provider: str
    model: str | None
    split: Literal["development", "holdout", "all"]
    workers: int
    isolation: Literal["expected_query_plan_then_answer"] = "expected_query_plan_then_answer"
    summary: AnswerEvaluationSummary
    results: list[AnswerCaseResult]


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _optional_check_rate(results: list[AnswerCaseResult], check: str) -> float | None:
    applicable = [result for result in results if check in result.checks]
    if not applicable:
        return None
    return _rate(sum(result.checks[check] for result in applicable), len(applicable))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _group_metrics(
    results: list[AnswerCaseResult],
    attribute: Literal["split", "category"],
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[AnswerCaseResult]] = {}
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


def _evidence_citations_present(
    answer: str,
    context,
    draft: GroundedAnswerDraft | None,
) -> bool:
    if draft is None:
        return False
    by_ref = {
        f"result_{index}": {field.canonical_field: field for field in product.fields}
        for index, product in enumerate(context.products, start=1)
    }
    for product in draft.products:
        for field_name in product.evidence_fields:
            evidence = by_ref[product.result_ref][field_name]
            required_fragments = [
                f"{evidence.source_id} 원본 행 {evidence.source_row}",
                f"기준일 {evidence.as_of.isoformat()}",
            ]
            if not all(fragment in answer for fragment in required_fragments):
                return False
    return True


class AnswerEvaluationRunner:
    def __init__(
        self,
        database_path: str | Path,
        provider: GroundedAnswerProvider,
        universe: list[NormalizedProductRecord] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.provider = provider
        self.oracle = SQLiteOracle(self.database_path)
        self.verifier = ResultVerifier()
        with connect_read_only(self.database_path) as connection:
            self.product_family = load_manifest(connection).dataset
        if universe is None:
            with connect_read_only(self.database_path) as connection:
                universe = load_all_records(connection)
        self.universe = universe

    def run_case(self, case: EvaluationCase) -> AnswerCaseResult:
        started = time.perf_counter()
        plan = case.expected_plan(self.product_family)
        if case.disposition is ExpectedDisposition.BLOCK:
            answer = render_blocked_plan(plan, self.product_family)
            finished = time.perf_counter()
            try:
                require_executable_search(plan)
            except PlanExecutionBlockedError:
                execution_blocked = True
            else:
                execution_blocked = False
            checks = {
                "execution_blocked": execution_blocked,
                "llm_not_called": True,
                "safe_answer": bool(answer.strip()),
            }
            return AnswerCaseResult(
                case_id=case.id,
                split=case.split,
                category=case.category,
                question=case.question,
                disposition=case.disposition,
                passed=all(checks.values()),
                mode="blocked",
                generation_latency_ms=0,
                total_latency_ms=round((finished - started) * 1000, 3),
                candidate_count=None,
                product_ids=[],
                checks=checks,
                violations=[],
                draft=None,
                answer=answer,
                error=None,
            )

        try:
            executed = self.oracle.execute(plan)
            verified = self.verifier.verify(plan, executed, self.universe)
            products = build_product_evidence(plan, verified)
            assert case.oracle is not None
            verified_product_ids = [product.product_id for product in products]
            context = build_grounded_answer_context(
                question=case.question,
                plan=plan,
                verified=verified,
                products=products,
            )
            composition = compose_grounded_answer(
                question=case.question,
                plan=plan,
                verified=verified,
                products=products,
                provider=self.provider,
            )
            nonempty = bool(products)
            checks = {
                "safe_answer": bool(composition.answer.strip()),
                "oracle_exact": (
                    verified.candidate_count == case.oracle.candidate_count
                    and verified_product_ids == case.oracle.top_product_ids
                ),
                "deterministic_core_preserved": (
                    context.deterministic_answer in composition.answer
                ),
                "source_date_covered": (
                    context.source_manifest.source_snapshot_date.isoformat() in composition.answer
                ),
            }
            if nonempty:
                checks.update(
                    {
                        "llm_grounded": composition.mode == "llm_grounded",
                        "product_order_exact": composition.verification.checks.get(
                            "product_order_exact", False
                        ),
                        "evidence_references_valid": all(
                            composition.verification.checks.get(name, False)
                            for name in (
                                "evidence_fields_exist",
                                "evidence_fields_usable",
                                "required_evidence_covered",
                            )
                        ),
                        "warning_coverage": composition.verification.checks.get(
                            "warning_codes_exact", False
                        ),
                        "numeric_fidelity": (
                            composition.verification.checks.get("prose_numbers_are_grounded", False)
                            and context.deterministic_answer in composition.answer
                        ),
                        "unsupported_claims_zero": (
                            composition.verification.checks.get("prose_numbers_are_grounded", False)
                            and composition.verification.checks.get(
                                "prose_has_no_product_identifiers", False
                            )
                            and composition.verification.checks.get(
                                "prose_has_no_advice_or_forecast", False
                            )
                        ),
                        "field_evidence_citations": _evidence_citations_present(
                            composition.answer,
                            context,
                            composition.draft,
                        ),
                    }
                )
            else:
                checks["empty_result_deterministic"] = composition.mode == "deterministic"
            finished = time.perf_counter()
            return AnswerCaseResult(
                case_id=case.id,
                split=case.split,
                category=case.category,
                question=case.question,
                disposition=case.disposition,
                passed=all(checks.values()),
                mode=composition.mode,
                generation_latency_ms=composition.generation_latency_ms,
                total_latency_ms=round((finished - started) * 1000, 3),
                candidate_count=verified.candidate_count,
                product_ids=[product.product_id for product in products],
                checks=checks,
                violations=composition.verification.violations,
                draft=composition.draft,
                answer=composition.answer,
                error=None,
            )
        except Exception as error:  # noqa: BLE001 - every case must become a result
            finished = time.perf_counter()
            return AnswerCaseResult(
                case_id=case.id,
                split=case.split,
                category=case.category,
                question=case.question,
                disposition=case.disposition,
                passed=False,
                mode="error",
                generation_latency_ms=0,
                total_latency_ms=round((finished - started) * 1000, 3),
                candidate_count=None,
                product_ids=[],
                checks={"safe_answer": False},
                violations=[],
                draft=None,
                answer="",
                error=f"{type(error).__name__}: {error}",
            )

    def run(self, cases: list[EvaluationCase], workers: int) -> list[AnswerCaseResult]:
        if workers < 1 or workers > 16:
            raise ValueError("workers must be in [1, 16]")
        if workers == 1:
            return [self.run_case(case) for case in cases]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(self.run_case, cases))


def build_answer_report(
    *,
    suite_id: str,
    suite_version: str,
    suite_sha256: str,
    database_sha256: str,
    manifest_sha256: str,
    provider: str,
    model: str | None,
    split: Literal["development", "holdout", "all"],
    workers: int,
    results: list[AnswerCaseResult],
) -> AnswerEvaluationReport:
    latencies = [
        result.generation_latency_ms for result in results if result.mode == "llm_grounded"
    ]
    executable = [result for result in results if result.disposition is ExpectedDisposition.EXECUTE]
    unsupported_claim_rate = _optional_check_rate(
        results,
        "unsupported_claims_zero",
    )
    summary = AnswerEvaluationSummary(
        total=len(results),
        passed=sum(result.passed for result in results),
        strict_accuracy=_rate(sum(result.passed for result in results), len(results)),
        executable_cases=len(executable),
        blocked_cases=sum(result.disposition is ExpectedDisposition.BLOCK for result in results),
        llm_grounded_cases=sum(result.mode == "llm_grounded" for result in results),
        deterministic_empty_cases=sum(result.mode == "deterministic" for result in results),
        fallback_cases=sum(result.mode == "deterministic_fallback" for result in results),
        safe_answer_rate=_optional_check_rate(results, "safe_answer") or 0.0,
        product_order_accuracy=_optional_check_rate(results, "product_order_exact"),
        evidence_reference_accuracy=_optional_check_rate(
            results,
            "evidence_references_valid",
        ),
        field_evidence_citation_rate=_optional_check_rate(
            results,
            "field_evidence_citations",
        ),
        numeric_fidelity_rate=_optional_check_rate(results, "numeric_fidelity"),
        warning_coverage_rate=_optional_check_rate(results, "warning_coverage"),
        source_date_coverage_rate=_optional_check_rate(results, "source_date_covered"),
        unsupported_claim_case_rate=(
            None if unsupported_claim_rate is None else round(1 - unsupported_claim_rate, 6)
        ),
        generation_latency_ms_p50=_percentile(latencies, 0.50),
        generation_latency_ms_p95=_percentile(latencies, 0.95),
        generation_latency_ms_max=round(max(latencies, default=0.0), 3),
        failures=[result.case_id for result in results if not result.passed],
        by_split=_group_metrics(results, "split"),
        by_category=_group_metrics(results, "category"),
    )
    return AnswerEvaluationReport(
        suite_id=suite_id,
        suite_version=suite_version,
        suite_sha256=suite_sha256,
        database_sha256=database_sha256,
        manifest_sha256=manifest_sha256,
        provider=provider,
        model=model,
        split=split,
        workers=workers,
        summary=summary,
        results=results,
    )
