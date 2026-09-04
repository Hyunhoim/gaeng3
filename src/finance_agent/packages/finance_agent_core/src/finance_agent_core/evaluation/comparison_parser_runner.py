from __future__ import annotations

import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
from finance_agent_core.evaluation.models import (
    EvaluationSplit,
    ExpectedBlocker,
    ExpectedDisposition,
)
from finance_agent_core.execution import (
    PlanExecutionBlockedError,
    ResultVerifier,
    SQLiteOracle,
    authorize_internal_evaluation_plan,
    build_fund_comparison,
    build_product_evidence,
    fund_comparison_product_ids,
    require_internal_evaluation_comparison,
)
from finance_agent_core.storage import connect_read_only, load_all_records, load_manifest

type ExpectedResolutionStatus = Literal[
    "resolved",
    "ambiguous",
    "not_found",
    "out_of_scope",
]


class ComparisonParserEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FundComparisonParserExpectation(ComparisonParserEvaluationModel):
    draft: FundComparisonDraft
    resolution_statuses: list[ExpectedResolutionStatus] = Field(max_length=4)
    resolved_product_ids: list[str] = Field(max_length=2)
    comparison_fields: list[str] = Field(max_length=10)
    disposition: ExpectedDisposition
    blocker: ExpectedBlocker | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> FundComparisonParserExpectation:
        if len(self.resolution_statuses) != len(self.draft.target_mentions):
            raise ValueError("resolution statuses must cover every target mention")
        if self.disposition is ExpectedDisposition.EXECUTE:
            if self.blocker is not None:
                raise ValueError("executable comparison must not declare a blocker")
            if len(self.resolved_product_ids) != 2:
                raise ValueError("executable comparison requires two resolved products")
            if len(set(self.resolved_product_ids)) != 2:
                raise ValueError("executable comparison products must be unique")
            if not self.comparison_fields:
                raise ValueError("executable comparison requires fields")
        elif self.blocker is None:
            raise ValueError("blocked comparison requires a blocker")
        return self


class FundComparisonParserCase(ComparisonParserEvaluationModel):
    id: str = Field(min_length=1, max_length=128)
    split: EvaluationSplit
    category: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=2000)
    expected: FundComparisonParserExpectation


class FundComparisonParserSuite(ComparisonParserEvaluationModel):
    suite_id: Literal["fund-compare-parser-core-24"]
    suite_version: Literal["1.0"]
    dataset: Literal["fund"]
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: list[FundComparisonParserCase] = Field(min_length=24, max_length=24)

    @model_validator(mode="after")
    def validate_suite(self) -> FundComparisonParserSuite:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("comparison parser case IDs must be unique")
        development = sum(case.split is EvaluationSplit.DEVELOPMENT for case in self.cases)
        holdout = sum(case.split is EvaluationSplit.HOLDOUT for case in self.cases)
        if (development, holdout) != (18, 6):
            raise ValueError("comparison parser suite requires 18 development and 6 holdout")
        return self


@dataclass(frozen=True)
class LoadedFundComparisonParserSuite:
    suite: FundComparisonParserSuite
    suite_sha256: str


class FundComparisonParserCaseResult(ComparisonParserEvaluationModel):
    case_id: str
    split: EvaluationSplit
    category: str
    question: str
    passed: bool
    generation_latency_ms: float = Field(ge=0)
    total_latency_ms: float = Field(ge=0)
    checks: dict[str, bool]
    draft: FundComparisonDraft | None
    resolution_statuses: list[ExpectedResolutionStatus]
    resolved_product_ids: list[str]
    comparison_fields: list[str]
    found_product_ids: list[str]
    error: str | None


class FundComparisonParserSummary(ComparisonParserEvaluationModel):
    total: int
    passed: int
    strict_accuracy: float
    draft_target_exact_rate: float
    draft_field_exact_rate: float
    mention_grounding_rate: float
    resolution_exact_rate: float
    plan_exact_rate: float
    oracle_exact_rate: float | None
    safety_block_rate: float | None
    generation_latency_ms_p50: float
    generation_latency_ms_p95: float
    generation_latency_ms_max: float
    failures: list[str]
    by_split: dict[str, dict[str, float | int]]
    by_category: dict[str, dict[str, float | int]]


class FundComparisonParserReport(ComparisonParserEvaluationModel):
    suite_id: str
    suite_version: str
    suite_sha256: str
    database_sha256: str
    manifest_sha256: str
    provider: str
    model: str | None
    split: Literal["development", "holdout", "all"]
    workers: int
    isolation: Literal["draft_then_deterministic_resolution_and_compare"] = (
        "draft_then_deterministic_resolution_and_compare"
    )
    summary: FundComparisonParserSummary
    results: list[FundComparisonParserCaseResult]


def load_fund_comparison_parser_suite() -> LoadedFundComparisonParserSuite:
    resource = files("finance_agent_core.evaluation.suites").joinpath(
        "fund_compare_parser_core_24.json"
    )
    raw = resource.read_bytes()
    return LoadedFundComparisonParserSuite(
        suite=FundComparisonParserSuite.model_validate(json.loads(raw)),
        suite_sha256=hashlib.sha256(raw).hexdigest(),
    )


class ExpectedFundComparisonDraftProvider:
    def __init__(self, cases: list[FundComparisonParserCase]) -> None:
        self._cases = {case.id: case for case in cases}

    @property
    def provider_name(self) -> Literal["expected"]:
        return "expected"

    @property
    def model_name(self) -> None:
        return None

    def generate_comparison_draft(
        self,
        question: str,
        question_id: str,
    ) -> FundComparisonDraft:
        case = self._cases[question_id]
        if case.question != question:
            raise ValueError("question text differs from the frozen comparison parser case")
        return case.expected.draft


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _group_metrics(
    results: list[FundComparisonParserCaseResult],
    attribute: Literal["split", "category"],
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[FundComparisonParserCaseResult]] = {}
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


def fund_comparison_plan_contract_exact(
    case: FundComparisonParserCase,
    compiled,
) -> bool:
    """Validate a compiled plan only against the frozen case contract."""

    plan = compiled.plan
    expected_ids = case.expected.resolved_product_ids
    expects_identity = (
        len(case.expected.draft.target_mentions) == 2
        and case.expected.resolution_statuses == ["resolved", "resolved"]
        and len(expected_ids) == 2
        and len(set(expected_ids)) == 2
    )
    expected_constraints: list[dict[str, object]] = [
        {
            "field": "public_offering",
            "operator": "eq",
            "value": True,
            "unit": "boolean",
            "strength": "locked",
        }
    ]
    if expects_identity:
        expected_constraints.append(
            {
                "field": "product_id",
                "operator": "in",
                "value": expected_ids,
                "unit": "code",
                "strength": "locked",
            }
        )
    actual_constraints = [
        {
            "field": constraint.field,
            "operator": constraint.operator.value,
            "value": constraint.value,
            "unit": constraint.unit,
            "strength": constraint.strength.value,
        }
        for constraint in plan.constraints
    ]
    fields = case.expected.comparison_fields
    expected_projection = list(
        dict.fromkeys(
            [
                "product_id",
                "product_name",
                "short_name",
                "fund_geography_scope",
                "fund_management_attribute",
                "risk_level",
                "three_month_return_pct",
                "aum",
                "trading_currency",
                "dynamic_as_of",
                *fields,
            ]
        )
    )
    if case.expected.disposition is ExpectedDisposition.EXECUTE:
        blocker_shape_exact = not plan.ambiguities and not plan.unsupported_conditions
    elif case.expected.blocker is ExpectedBlocker.AMBIGUITY:
        blocker_shape_exact = bool(plan.ambiguities) and not plan.unsupported_conditions
    else:
        blocker_shape_exact = bool(plan.unsupported_conditions) and not plan.ambiguities
    payload = plan.intent_payload
    return all(
        (
            plan.schema_version == "1.0",
            plan.question_id == case.id,
            plan.intent.value == "compare",
            [family.value for family in plan.product_families] == ["fund"],
            actual_constraints == expected_constraints,
            plan.ranking == [],
            plan.projection == expected_projection,
            plan.limit == 2,
            payload.comparison_fields == fields,
            payload.group_by == [],
            payload.aggregations == [],
            payload.explain_product_ids == [],
            blocker_shape_exact,
        )
    )


class FundComparisonParserEvaluationRunner:
    def __init__(
        self,
        database_path: str | Path,
        provider: FundComparisonDraftProvider,
    ) -> None:
        if provider.provider_name not in {"expected", "local_test"}:
            raise ValueError("comparison parser evaluation requires expected or local_test")
        self.database_path = Path(database_path)
        self.provider = provider
        self.oracle = SQLiteOracle(self.database_path)
        self.verifier = ResultVerifier()
        with connect_read_only(self.database_path) as connection:
            manifest = load_manifest(connection)
            self.universe = load_all_records(connection)
        if manifest.dataset != "fund":
            raise ValueError("comparison parser evaluation requires a fund database")
        self.resolver = FundProductResolver(self.universe)

    def run_case(
        self,
        case: FundComparisonParserCase,
    ) -> FundComparisonParserCaseResult:
        started = time.perf_counter()
        generated_at = started
        draft: FundComparisonDraft | None = None
        resolution_statuses: list[ExpectedResolutionStatus] = []
        resolved_product_ids: list[str] = []
        comparison_fields: list[str] = []
        found_product_ids: list[str] = []
        checks: dict[str, bool] = {"draft_schema_valid": False}
        error: str | None = None
        try:
            draft = self.provider.generate_comparison_draft(case.question, case.id)
            generated_at = time.perf_counter()
            checks["draft_schema_valid"] = True
            checks["draft_targets_exact"] = (
                draft.target_mentions == case.expected.draft.target_mentions
            )
            checks["draft_fields_exact"] = (
                draft.comparison_fields == case.expected.draft.comparison_fields
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
            checks["mentions_grounded"] = all(compiled.mentions_grounded)
            checks["resolution_statuses_exact"] = (
                resolution_statuses == case.expected.resolution_statuses
            )
            checks["resolved_product_ids_exact"] = (
                resolved_product_ids == case.expected.resolved_product_ids
            )
            checks["comparison_fields_exact"] = comparison_fields == case.expected.comparison_fields
            checks["question_targets_complete"] = compiled.targets_complete
            checks["target_roles_unambiguous"] = compiled.target_roles_unambiguous
            checks["plan_exact"] = fund_comparison_plan_contract_exact(
                case,
                compiled,
            )

            if case.expected.disposition is ExpectedDisposition.EXECUTE:
                require_internal_evaluation_comparison(compiled.plan)
                checks["execution_allowed"] = True
                checks["identity_constraint_exact"] = (
                    fund_comparison_product_ids(compiled.plan) == case.expected.resolved_product_ids
                )
                validated_plan = authorize_internal_evaluation_plan(
                    compiled.plan,
                    self.database_path,
                )
                executed = self.oracle.execute(validated_plan)
                verified = self.verifier.verify(
                    compiled.plan,
                    executed,
                    self.universe,
                )
                evidence = build_product_evidence(compiled.plan, verified)
                comparison = build_fund_comparison(compiled.plan, verified, evidence)
                found_product_ids = list(comparison.found_product_ids)
                checks["verifier_passed"] = True
                checks["oracle_exact"] = (
                    found_product_ids == case.expected.resolved_product_ids
                    and not comparison.missing_product_ids
                    and comparison.verified.candidate_count == 2
                )
            else:
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
        except Exception as exception:  # noqa: BLE001 - every case becomes a result
            if generated_at == started:
                generated_at = time.perf_counter()
            error = f"{type(exception).__name__}: {exception}"
        finished = time.perf_counter()
        return FundComparisonParserCaseResult(
            case_id=case.id,
            split=case.split,
            category=case.category,
            question=case.question,
            passed=error is None and all(checks.values()),
            generation_latency_ms=round((generated_at - started) * 1000, 3),
            total_latency_ms=round((finished - started) * 1000, 3),
            checks=checks,
            draft=draft,
            resolution_statuses=resolution_statuses,
            resolved_product_ids=resolved_product_ids,
            comparison_fields=comparison_fields,
            found_product_ids=found_product_ids,
            error=error,
        )

    def run(
        self,
        cases: list[FundComparisonParserCase],
        workers: int = 1,
    ) -> list[FundComparisonParserCaseResult]:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        if workers == 1:
            return [self.run_case(case) for case in cases]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(self.run_case, case) for case in cases]
            return [future.result() for future in futures]


def _summary(
    results: list[FundComparisonParserCaseResult],
) -> FundComparisonParserSummary:
    executable = [result for result in results if "oracle_exact" in result.checks]
    blocked = [result for result in results if "execution_blocked" in result.checks]
    latencies = [result.generation_latency_ms for result in results]
    return FundComparisonParserSummary(
        total=len(results),
        passed=sum(result.passed for result in results),
        strict_accuracy=_rate(sum(result.passed for result in results), len(results)),
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
                sum(result.checks["oracle_exact"] for result in executable),
                len(executable),
            )
            if executable
            else None
        ),
        safety_block_rate=(
            _rate(
                sum(result.checks["execution_blocked"] for result in blocked),
                len(blocked),
            )
            if blocked
            else None
        ),
        generation_latency_ms_p50=_percentile(latencies, 0.50),
        generation_latency_ms_p95=_percentile(latencies, 0.95),
        generation_latency_ms_max=round(max(latencies, default=0.0), 3),
        failures=[result.case_id for result in results if not result.passed],
        by_split=_group_metrics(results, "split"),
        by_category=_group_metrics(results, "category"),
    )


def build_fund_comparison_parser_report(
    *,
    loaded: LoadedFundComparisonParserSuite,
    provider: FundComparisonDraftProvider,
    split: Literal["development", "holdout", "all"],
    workers: int,
    results: list[FundComparisonParserCaseResult],
) -> FundComparisonParserReport:
    return FundComparisonParserReport(
        suite_id=loaded.suite.suite_id,
        suite_version=loaded.suite.suite_version,
        suite_sha256=loaded.suite_sha256,
        database_sha256=loaded.suite.database_sha256,
        manifest_sha256=loaded.suite.manifest_sha256,
        provider=provider.provider_name,
        model=provider.model_name,
        split=split,
        workers=workers,
        summary=_summary(results),
        results=results,
    )
