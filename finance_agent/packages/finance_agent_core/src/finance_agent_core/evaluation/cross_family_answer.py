from __future__ import annotations

from typing import Literal

from pydantic import Field

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.answering import (
    GroundedAnswerContext,
    GroundedAnswerDraft,
    GroundedAnswerProvider,
)
from finance_agent_core.contracts.backend import (
    BackendAnswerMode,
    BackendStatus,
    routed_result_to_backend,
)
from finance_agent_core.contracts.routing import RouteDisposition
from finance_agent_core.evaluation.cross_family_search import (
    CrossFamilyCategory,
    CrossFamilyDataContract,
    CrossFamilyEvaluationModel,
    CrossFamilyExpectedFamily,
    CrossFamilySearchCase,
    LoadedCrossFamilySearchSuite,
)


class CountingGroundedAnswerProvider:
    """Count actual generation calls without changing the provider contract."""

    def __init__(self, delegate: GroundedAnswerProvider) -> None:
        self.delegate = delegate
        self.call_count = 0

    @property
    def provider_name(self) -> str:
        return self.delegate.provider_name

    @property
    def model_name(self) -> str | None:
        return self.delegate.model_name

    def generate_grounded_answer(
        self,
        context: GroundedAnswerContext,
    ) -> GroundedAnswerDraft:
        self.call_count += 1
        return self.delegate.generate_grounded_answer(context)


class CrossFamilyAnswerCaseResult(CrossFamilyEvaluationModel):
    case_id: str
    category: CrossFamilyCategory
    passed: bool
    checks: dict[str, bool]
    actual_disposition: RouteDisposition
    actual_backend_status: BackendStatus
    answer_mode: BackendAnswerMode
    fallback_used: bool
    verification_passed: bool | None
    model_calls: int = Field(ge=0)
    generation_latency_ms: float = Field(ge=0)


class CrossFamilyAnswerSummary(CrossFamilyEvaluationModel):
    total: int
    passed: int
    strict_accuracy: float
    generation_eligible: int
    llm_grounded: int
    deterministic_fallback: int
    fallback_rate: float
    model_calls: int
    generation_latency_ms: float
    failures: list[str]


class CrossFamilyAnswerReport(CrossFamilyEvaluationModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: str
    suite_version: str
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["public_grounded_regression_not_blind"] = "public_grounded_regression_not_blind"
    provider: str
    model: str | None
    data: dict[str, CrossFamilyDataContract]
    summary: CrossFamilyAnswerSummary
    results: list[CrossFamilyAnswerCaseResult]


def evaluate_cross_family_answer_case(
    agent: RoutedFinanceAgent,
    provider: CountingGroundedAnswerProvider,
    case: CrossFamilySearchCase,
) -> CrossFamilyAnswerCaseResult:
    calls_before = provider.call_count
    routed = agent.answer(case.question, case.id)
    backend = routed_result_to_backend(routed)
    actual_families = [
        CrossFamilyExpectedFamily(
            product_family=item.product_family,
            status=item.status,
            candidate_count=item.candidate_count,
            top_product_ids=[product.product_id for product in item.products],
        )
        for item in routed.family_searches
    ]
    model_calls = provider.call_count - calls_before
    expected_model_calls = sum(family.status == "success" for family in case.expected_families)
    generation_eligible = expected_model_calls > 0
    expected_answer_mode = (
        BackendAnswerMode.LLM_GROUNDED
        if generation_eligible
        else (
            BackendAnswerMode.DETERMINISTIC
            if case.expected_disposition is RouteDisposition.EXECUTE
            else BackendAnswerMode.CONTROL
        )
    )
    verification_passed = (
        routed.answer_composition.verification.passed
        if routed.answer_composition is not None
        else None
    )
    checks = {
        "disposition": routed.decision.disposition is case.expected_disposition,
        "backend_status": backend.status is case.expected_backend_status,
        "reason_code": routed.decision.reason_code == case.expected_reason_code,
        "candidate_count": routed.candidate_count == case.expected_candidate_count,
        "family_results": actual_families == case.expected_families,
        "provider_call_contract": model_calls == expected_model_calls,
        "answer_mode_contract": backend.answer_mode is expected_answer_mode,
        "verification_contract": (
            verification_passed is True
            if case.expected_disposition is RouteDisposition.EXECUTE
            else verification_passed is None
        ),
        "cross_family_numeric_safety": (
            "직접 비교·합산·우열 판단은 수행하지 않았습니다" in routed.answer
            if case.expected_families
            else routed.status != "executed"
        ),
    }
    return CrossFamilyAnswerCaseResult(
        case_id=case.id,
        category=case.category,
        passed=all(checks.values()),
        checks=checks,
        actual_disposition=routed.decision.disposition,
        actual_backend_status=backend.status,
        answer_mode=backend.answer_mode,
        fallback_used=backend.fallback_used,
        verification_passed=verification_passed,
        model_calls=model_calls,
        generation_latency_ms=(
            routed.answer_composition.generation_latency_ms
            if routed.answer_composition is not None
            else 0
        ),
    )


def run_cross_family_answer_suite(
    loaded: LoadedCrossFamilySearchSuite,
    agent: RoutedFinanceAgent,
    provider: CountingGroundedAnswerProvider,
) -> CrossFamilyAnswerReport:
    results = [
        evaluate_cross_family_answer_case(agent, provider, case) for case in loaded.suite.cases
    ]
    passed = sum(result.passed for result in results)
    generation_results = [
        result for result in results if result.category in {"all_success", "partial_success"}
    ]
    fallback_count = sum(result.fallback_used for result in generation_results)
    return CrossFamilyAnswerReport(
        suite_id=loaded.suite.suite_id,
        suite_version=loaded.suite.suite_version,
        suite_sha256=loaded.suite_sha256,
        provider=provider.provider_name,
        model=provider.model_name,
        data={family.value: contract for family, contract in loaded.suite.data.items()},
        summary=CrossFamilyAnswerSummary(
            total=len(results),
            passed=passed,
            strict_accuracy=round(passed / len(results), 6),
            generation_eligible=len(generation_results),
            llm_grounded=sum(
                result.answer_mode is BackendAnswerMode.LLM_GROUNDED
                for result in generation_results
            ),
            deterministic_fallback=fallback_count,
            fallback_rate=round(
                fallback_count / len(generation_results),
                6,
            ),
            model_calls=sum(result.model_calls for result in results),
            generation_latency_ms=round(
                sum(result.generation_latency_ms for result in results),
                3,
            ),
            failures=[result.case_id for result in results if not result.passed],
        ),
        results=results,
    )
