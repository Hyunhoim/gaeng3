from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.contracts.backend import (
    BackendStatus,
    routed_result_to_backend,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import RouteDisposition

type CrossFamilyCategory = Literal[
    "all_success",
    "partial_success",
    "all_not_found",
    "forbidden_cross_family_comparison",
]


class CrossFamilyEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CrossFamilyDataContract(CrossFamilyEvaluationModel):
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CrossFamilyExpectedFamily(CrossFamilyEvaluationModel):
    product_family: ProductFamily
    status: Literal["success", "not_found"]
    candidate_count: int = Field(ge=0)
    top_product_ids: list[str] = Field(max_length=10)

    @model_validator(mode="after")
    def validate_status(self) -> CrossFamilyExpectedFamily:
        if (self.status == "not_found") != (self.candidate_count == 0):
            raise ValueError("expected family status and count disagree")
        if self.status == "not_found" and self.top_product_ids:
            raise ValueError("not_found family cannot expect products")
        return self


class CrossFamilySearchCase(CrossFamilyEvaluationModel):
    id: str = Field(pattern=r"^cross-family-search-v1-\d{3}$")
    category: CrossFamilyCategory
    question: str = Field(min_length=5, max_length=2000)
    expected_disposition: RouteDisposition
    expected_backend_status: BackendStatus
    expected_reason_code: str
    expected_candidate_count: int | None = Field(default=None, ge=0)
    expected_families: list[CrossFamilyExpectedFamily] = Field(max_length=4)

    @model_validator(mode="after")
    def validate_case_shape(self) -> CrossFamilySearchCase:
        executable = self.expected_disposition is RouteDisposition.EXECUTE
        if executable != (len(self.expected_families) >= 2):
            raise ValueError("only executable cases may contain family expectations")
        if executable != (self.expected_candidate_count is not None):
            raise ValueError("only executable cases require candidate_count")
        return self


class CrossFamilySearchSuite(CrossFamilyEvaluationModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["cross-family-search-v1-4"]
    suite_version: Literal["1.0"]
    status: Literal["public_deterministic_regression_not_blind"]
    data: dict[ProductFamily, CrossFamilyDataContract]
    cases: list[CrossFamilySearchCase] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_coverage(self) -> CrossFamilySearchSuite:
        if set(self.data) != {
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        }:
            raise ValueError("suite data must cover domestic and overseas ETP")
        if {case.category for case in self.cases} != {
            "all_success",
            "partial_success",
            "all_not_found",
            "forbidden_cross_family_comparison",
        }:
            raise ValueError("suite must cover all cross-family safety categories")
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        return self


class LoadedCrossFamilySearchSuite(CrossFamilyEvaluationModel):
    suite: CrossFamilySearchSuite
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CrossFamilyCaseResult(CrossFamilyEvaluationModel):
    case_id: str
    category: CrossFamilyCategory
    passed: bool
    checks: dict[str, bool]
    actual_disposition: RouteDisposition
    actual_backend_status: BackendStatus
    actual_reason_code: str
    actual_candidate_count: int | None
    actual_families: list[CrossFamilyExpectedFamily]


class CrossFamilySearchSummary(CrossFamilyEvaluationModel):
    total: int
    passed: int
    strict_accuracy: float
    failures: list[str]


class CrossFamilySearchReport(CrossFamilyEvaluationModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: str
    suite_version: str
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["public_deterministic_regression_not_blind"]
    provider: Literal["deterministic_server"] = "deterministic_server"
    data: dict[ProductFamily, CrossFamilyDataContract]
    summary: CrossFamilySearchSummary
    results: list[CrossFamilyCaseResult]


def load_cross_family_search_suite() -> LoadedCrossFamilySearchSuite:
    resource = files("finance_agent_core.evaluation.suites").joinpath("cross_family_search_v1.json")
    raw = resource.read_bytes()
    return LoadedCrossFamilySearchSuite(
        suite=CrossFamilySearchSuite.model_validate(json.loads(raw)),
        suite_sha256=hashlib.sha256(raw).hexdigest(),
    )


def evaluate_cross_family_case(
    agent: RoutedFinanceAgent,
    case: CrossFamilySearchCase,
) -> CrossFamilyCaseResult:
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
    checks = {
        "disposition": routed.decision.disposition is case.expected_disposition,
        "backend_status": backend.status is case.expected_backend_status,
        "reason_code": routed.decision.reason_code == case.expected_reason_code,
        "candidate_count": routed.candidate_count == case.expected_candidate_count,
        "family_results": actual_families == case.expected_families,
        "top_level_plan_absent": routed.query_plan is None,
        "cross_family_numeric_safety": (
            "직접 비교·합산·우열 판단은 수행하지 않았습니다" in routed.answer
            if case.expected_families
            else routed.status != "executed"
        ),
    }
    return CrossFamilyCaseResult(
        case_id=case.id,
        category=case.category,
        passed=all(checks.values()),
        checks=checks,
        actual_disposition=routed.decision.disposition,
        actual_backend_status=backend.status,
        actual_reason_code=routed.decision.reason_code,
        actual_candidate_count=routed.candidate_count,
        actual_families=actual_families,
    )


def run_cross_family_search_suite(
    loaded: LoadedCrossFamilySearchSuite,
    agent: RoutedFinanceAgent,
) -> CrossFamilySearchReport:
    results = [evaluate_cross_family_case(agent, case) for case in loaded.suite.cases]
    passed = sum(result.passed for result in results)
    return CrossFamilySearchReport(
        suite_id=loaded.suite.suite_id,
        suite_version=loaded.suite.suite_version,
        suite_sha256=loaded.suite_sha256,
        status=loaded.suite.status,
        data=loaded.suite.data,
        summary=CrossFamilySearchSummary(
            total=len(results),
            passed=passed,
            strict_accuracy=round(passed / len(results), 6),
            failures=[result.case_id for result in results if not result.passed],
        ),
        results=results,
    )
