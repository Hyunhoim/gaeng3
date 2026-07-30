from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    RouteDecision,
    RouteDisposition,
)

DIAGNOSTIC_SUITE_ID = "pre-hcx-route-diagnostic-28-v3"
DIAGNOSTIC_CASE_COUNT = 28
DIAGNOSTIC_SUITE_PATTERN = r"^pre-hcx-route-diagnostic-28(?:-v[23])?$"


class DiagnosticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiagnosticCase(DiagnosticModel):
    id: str = Field(pattern=r"^pre-hcx-\d{3}$")
    product_family: ProductFamily
    intent: InteractionIntent
    question: str = Field(min_length=5, max_length=1000)
    expected_disposition: RouteDisposition
    expected_query_plan_intent: Intent | None

    @model_validator(mode="after")
    def validate_expected_route(self) -> DiagnosticCase:
        if self.expected_disposition is RouteDisposition.EXECUTE:
            if self.expected_query_plan_intent is None:
                raise ValueError("executable diagnostic cases require a QueryPlan intent")
        elif self.expected_query_plan_intent is not None:
            raise ValueError("control diagnostic cases cannot require a QueryPlan intent")
        return self


class DiagnosticSuite(DiagnosticModel):
    schema_version: Literal["1.0"]
    suite_id: str = Field(pattern=DIAGNOSTIC_SUITE_PATTERN)
    suite_version: Literal["1.0", "2.0", "3.0"]
    status: Literal["internal_diagnostic_not_blind"]
    author_role: Literal["ai_engineering"]
    cases: list[DiagnosticCase] = Field(
        min_length=DIAGNOSTIC_CASE_COUNT,
        max_length=DIAGNOSTIC_CASE_COUNT,
    )

    @model_validator(mode="after")
    def validate_coverage(self) -> DiagnosticSuite:
        expected_version = {
            "pre-hcx-route-diagnostic-28-v3": "3.0",
            "pre-hcx-route-diagnostic-28-v2": "2.0",
        }.get(self.suite_id, "1.0")
        if self.suite_version != expected_version:
            raise ValueError("diagnostic suite id and version differ")
        expected_ids = [f"pre-hcx-{index:03d}" for index in range(1, 29)]
        if [case.id for case in self.cases] != expected_ids:
            raise ValueError("diagnostic case ids must be ordered from 001 through 028")
        pairs = [(case.product_family, case.intent) for case in self.cases]
        expected_pairs = {
            (family, intent) for family in ProductFamily for intent in InteractionIntent
        }
        if len(pairs) != len(set(pairs)) or set(pairs) != expected_pairs:
            raise ValueError("diagnostic suite must cover every family/intent pair exactly once")
        questions = ["".join(case.question.casefold().split()) for case in self.cases]
        if len(questions) != len(set(questions)):
            raise ValueError("diagnostic questions must be unique after whitespace normalization")
        return self


class DiagnosticCommitment(DiagnosticModel):
    protocol_version: Literal["1.0"]
    suite_id: str = Field(pattern=DIAGNOSTIC_SUITE_PATTERN)
    status: Literal["sealed_internal_diagnostic"]
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_count: Literal[28]
    created_at_utc: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    limitation: Literal["self_authored_not_independent_blind"]


class DiagnosticCaseResult(DiagnosticModel):
    case_id: str
    passed: bool
    checks: dict[str, bool]
    expected_intent: InteractionIntent
    actual_intent: InteractionIntent
    expected_disposition: RouteDisposition
    actual_disposition: RouteDisposition
    reason_code: str


class DiagnosticSummary(DiagnosticModel):
    total: int
    passed: int
    strict_accuracy: float
    by_product_family: dict[str, dict[str, int | float]]
    by_intent: dict[str, dict[str, int | float]]
    failures: list[str]


class DiagnosticReport(DiagnosticModel):
    schema_version: Literal["1.0"]
    suite_id: str
    suite_version: str
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: Literal["pre_router_snapshot", "current_router"]
    router_version: str
    generated_at_utc: str
    summary: DiagnosticSummary
    results: list[DiagnosticCaseResult]


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_diagnostic_suite() -> tuple[DiagnosticSuite, str]:
    resource = files("finance_agent_core.evaluation.suites").joinpath(
        "pre_hcx_route_diagnostic_28_v3.json"
    )
    raw = resource.read_bytes()
    return DiagnosticSuite.model_validate(json.loads(raw)), hashlib.sha256(raw).hexdigest()


def create_diagnostic_commitment(
    suite_path: Path,
    *,
    created_at_utc: str | None = None,
) -> DiagnosticCommitment:
    suite = DiagnosticSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))
    created_at = created_at_utc or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return DiagnosticCommitment(
        protocol_version="1.0",
        suite_id=suite.suite_id,
        status="sealed_internal_diagnostic",
        suite_sha256=sha256_path(suite_path),
        case_count=len(suite.cases),
        created_at_utc=created_at,
        limitation="self_authored_not_independent_blind",
    )


def verify_diagnostic_commitment(
    commitment: DiagnosticCommitment,
    suite_path: Path,
) -> None:
    suite = DiagnosticSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))
    if suite.suite_id != commitment.suite_id:
        raise ValueError("diagnostic suite id differs from the commitment")
    if sha256_path(suite_path) != commitment.suite_sha256:
        raise ValueError("diagnostic suite hash differs from the sealed commitment")


def evaluate_decisions(
    suite: DiagnosticSuite,
    decisions: list[RouteDecision],
    *,
    suite_sha256: str,
    profile: Literal["pre_router_snapshot", "current_router"],
    router_version: str,
    generated_at_utc: str,
) -> DiagnosticReport:
    if len(decisions) != len(suite.cases):
        raise ValueError("one route decision is required for every diagnostic case")
    results: list[DiagnosticCaseResult] = []
    for case, decision in zip(suite.cases, decisions, strict=True):
        actual_families = decision.draft.product_families
        checks = {
            "request_id": decision.draft.request_id == case.id,
            "question_exact": decision.draft.question == case.question,
            "intent": decision.draft.intent is case.intent,
            "product_family": actual_families == [case.product_family],
            "disposition": decision.disposition is case.expected_disposition,
            "query_plan_intent": (decision.query_plan_intent is case.expected_query_plan_intent),
        }
        results.append(
            DiagnosticCaseResult(
                case_id=case.id,
                passed=all(checks.values()),
                checks=checks,
                expected_intent=case.intent,
                actual_intent=decision.draft.intent,
                expected_disposition=case.expected_disposition,
                actual_disposition=decision.disposition,
                reason_code=decision.reason_code,
            )
        )

    def grouped(
        key_values: list[str],
    ) -> dict[str, dict[str, int | float]]:
        totals = Counter(key_values)
        passed = Counter(
            value for value, result in zip(key_values, results, strict=True) if result.passed
        )
        return {
            key: {
                "total": total,
                "passed": passed[key],
                "accuracy": round(passed[key] / total, 6),
            }
            for key, total in sorted(totals.items())
        }

    passed_count = sum(result.passed for result in results)
    return DiagnosticReport(
        schema_version="1.0",
        suite_id=suite.suite_id,
        suite_version=suite.suite_version,
        suite_sha256=suite_sha256,
        profile=profile,
        router_version=router_version,
        generated_at_utc=generated_at_utc,
        summary=DiagnosticSummary(
            total=len(results),
            passed=passed_count,
            strict_accuracy=round(passed_count / len(results), 6),
            by_product_family=grouped([case.product_family.value for case in suite.cases]),
            by_intent=grouped([case.intent.value for case in suite.cases]),
            failures=[result.case_id for result in results if not result.passed],
        ),
        results=results,
    )
