from __future__ import annotations

import hashlib
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_agent_core.agent.providers import QueryPlanProvider
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.domain import NormalizedProductRecord
from finance_agent_core.evaluation.models import (
    EvaluationCase,
    EvaluationSplit,
    ExpectedDisposition,
)
from finance_agent_core.evaluation.scoring import semantic_checks, stable_plan_payload
from finance_agent_core.execution import (
    PlanExecutionBlockedError,
    ResultVerifier,
    SQLiteOracle,
    authorize_internal_evaluation_plan,
    require_executable_search,
    require_internal_evaluation_search,
)
from finance_agent_core.storage import connect_read_only, load_all_records, load_manifest


class EvaluationResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaseEvaluationResult(EvaluationResultModel):
    case_id: str
    split: EvaluationSplit
    category: str
    question: str
    passed: bool
    generation_latency_ms: float = Field(ge=0)
    total_latency_ms: float = Field(ge=0)
    checks: dict[str, bool]
    generated_plan: dict[str, object] | None
    candidate_count: int | None
    top_product_ids: list[str]
    error: str | None


class EvaluationSummary(EvaluationResultModel):
    total: int
    passed: int
    strict_accuracy: float
    valid_plan_rate: float
    plan_exact_rate: float
    constraint_exact_rate: float
    oracle_exact_rate: float | None
    safety_block_rate: float | None
    generation_latency_ms_p50: float
    generation_latency_ms_p95: float
    generation_latency_ms_max: float
    failures: list[str]
    by_split: dict[str, dict[str, float | int]]
    by_category: dict[str, dict[str, float | int]]


class EvaluationReport(EvaluationResultModel):
    suite_id: str
    suite_version: str
    suite_sha256: str
    database_sha256: str
    manifest_sha256: str
    provider: str
    model: str | None
    split: Literal["development", "holdout", "all"]
    workers: int
    summary: EvaluationSummary
    results: list[CaseEvaluationResult]


class ExpectedPlanProvider:
    def __init__(
        self,
        cases: list[EvaluationCase],
        product_family: str = "overseas_etp",
    ) -> None:
        self._cases = {case.id: case for case in cases}
        self.product_family = product_family

    @property
    def provider_name(self) -> Literal["expected"]:
        return "expected"

    def generate_query_plan(self, question: str, question_id: str) -> QueryPlan:
        case = self._cases[question_id]
        if case.question != question:
            raise ValueError("question text differs from the frozen evaluation case")
        return case.expected_plan(self.product_family)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _group_metrics(
    results: list[CaseEvaluationResult],
    attribute: Literal["split", "category"],
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[CaseEvaluationResult]] = {}
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


def _summary(results: list[CaseEvaluationResult]) -> EvaluationSummary:
    execute_results = [result for result in results if "oracle_exact" in result.checks]
    blocked_results = [result for result in results if "execution_blocked" in result.checks]
    latencies = [result.generation_latency_ms for result in results]
    return EvaluationSummary(
        total=len(results),
        passed=sum(result.passed for result in results),
        strict_accuracy=_rate(sum(result.passed for result in results), len(results)),
        valid_plan_rate=_rate(
            sum(result.checks.get("schema_valid", False) for result in results),
            len(results),
        ),
        plan_exact_rate=_rate(
            sum(result.checks.get("plan_exact", False) for result in results),
            len(results),
        ),
        constraint_exact_rate=_rate(
            sum(result.checks.get("constraints", False) for result in results),
            len(results),
        ),
        oracle_exact_rate=(
            _rate(
                sum(result.checks["oracle_exact"] for result in execute_results),
                len(execute_results),
            )
            if execute_results
            else None
        ),
        safety_block_rate=(
            _rate(
                sum(result.checks["execution_blocked"] for result in blocked_results),
                len(blocked_results),
            )
            if blocked_results
            else None
        ),
        generation_latency_ms_p50=_percentile(latencies, 0.50),
        generation_latency_ms_p95=_percentile(latencies, 0.95),
        generation_latency_ms_max=round(max(latencies, default=0.0), 3),
        failures=[result.case_id for result in results if not result.passed],
        by_split=_group_metrics(results, "split"),
        by_category=_group_metrics(results, "category"),
    )


class EvaluationRunner:
    def __init__(
        self,
        database_path: str | Path,
        provider: QueryPlanProvider | ExpectedPlanProvider,
        universe: list[NormalizedProductRecord] | None = None,
        *,
        allow_internal_disabled_dataset: bool = False,
    ) -> None:
        self.database_path = Path(database_path)
        self.provider = provider
        self.oracle = SQLiteOracle(self.database_path)
        self.verifier = ResultVerifier()
        with connect_read_only(self.database_path) as connection:
            self.product_family = load_manifest(connection).dataset
        if allow_internal_disabled_dataset:
            if provider.provider_name not in {"expected", "local_test"}:
                raise ValueError(
                    "disabled-dataset evaluation is restricted to expected or local_test"
                )
            if self.product_family != "fund":
                raise ValueError(
                    "disabled-dataset evaluation is restricted to the fund approval gate"
                )
        self.allow_internal_disabled_dataset = allow_internal_disabled_dataset
        if universe is None:
            with connect_read_only(self.database_path) as connection:
                universe = load_all_records(connection)
        self.universe = universe

    def _require_search(self, plan: QueryPlan) -> None:
        if self.allow_internal_disabled_dataset:
            require_internal_evaluation_search(plan)
        else:
            require_executable_search(plan)

    def run_case(self, case: EvaluationCase) -> CaseEvaluationResult:
        started = time.perf_counter()
        generated_at = started
        plan: QueryPlan | None = None
        checks: dict[str, bool] = {"schema_valid": False}
        candidate_count: int | None = None
        top_product_ids: list[str] = []
        error: str | None = None
        try:
            plan = self.provider.generate_query_plan(case.question, case.id)
            generated_at = time.perf_counter()
            checks["schema_valid"] = True
            checks.update(semantic_checks(case, plan, self.product_family))
            if case.disposition is ExpectedDisposition.EXECUTE:
                try:
                    self._require_search(plan)
                    checks["execution_allowed"] = True
                except PlanExecutionBlockedError as exception:
                    checks["execution_allowed"] = False
                    raise exception
                validated_plan = authorize_internal_evaluation_plan(
                    plan,
                    self.database_path,
                )
                executed = self.oracle.execute(validated_plan)
                verified = self.verifier.verify(plan, executed, self.universe)
                checks["verifier"] = True
                candidate_count = verified.candidate_count
                top_product_ids = [record.product_id for record in verified.records]
                assert case.oracle is not None
                checks["candidate_count"] = candidate_count == case.oracle.candidate_count
                checks["top_product_ids"] = top_product_ids == case.oracle.top_product_ids
                checks["oracle_exact"] = checks["candidate_count"] and checks["top_product_ids"]
            else:
                try:
                    self._require_search(plan)
                except PlanExecutionBlockedError:
                    checks["execution_blocked"] = True
                else:
                    checks["execution_blocked"] = False
        except Exception as exception:  # noqa: BLE001 - every case must become a result
            if generated_at == started:
                generated_at = time.perf_counter()
            error = f"{type(exception).__name__}: {exception}"
            if case.disposition is ExpectedDisposition.EXECUTE:
                checks.setdefault("execution_allowed", False)
                checks.setdefault("verifier", False)
                checks.setdefault("candidate_count", False)
                checks.setdefault("top_product_ids", False)
                checks.setdefault("oracle_exact", False)
            else:
                checks.setdefault("execution_blocked", False)
        finished = time.perf_counter()
        passed = all(checks.values())
        return CaseEvaluationResult(
            case_id=case.id,
            split=case.split,
            category=case.category,
            question=case.question,
            passed=passed,
            generation_latency_ms=round((generated_at - started) * 1000, 3),
            total_latency_ms=round((finished - started) * 1000, 3),
            checks=checks,
            generated_plan=stable_plan_payload(plan) if plan is not None else None,
            candidate_count=candidate_count,
            top_product_ids=top_product_ids,
            error=error,
        )

    def run(self, cases: list[EvaluationCase], workers: int) -> list[CaseEvaluationResult]:
        if workers < 1 or workers > 16:
            raise ValueError("workers must be in [1, 16]")
        if workers == 1:
            return [self.run_case(case) for case in cases]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(self.run_case, cases))


def build_report(
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
    results: list[CaseEvaluationResult],
) -> EvaluationReport:
    return EvaluationReport(
        suite_id=suite_id,
        suite_version=suite_version,
        suite_sha256=suite_sha256,
        database_sha256=database_sha256,
        manifest_sha256=manifest_sha256,
        provider=provider,
        model=model,
        split=split,
        workers=workers,
        summary=_summary(results),
        results=results,
    )
