from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from finance_agent_core.agent.compiler import (
    PlanCompilationBlockedError,
    ServerQueryPlanCompiler,
)
from finance_agent_core.agent.grounded_planning import (
    GroundedPlanGate,
    GroundedPlanProposal,
    GroundedPlanProvider,
    GroundedPlanRejectedError,
    grounded_plan_is_eligible,
)
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.queryplan import ProductFamily, QueryPlan
from finance_agent_core.evaluation.metamorphic import (
    MetamorphicModel,
    MutationBatch,
    MutationCandidate,
    mutation_batch_semantic_sha256,
)
from finance_agent_core.storage import ProductIdentitySnapshotCache, RecordSnapshotCache

type GroundedPlanOutcome = Literal[
    "model_rescue",
    "model_supplement",
    "model_same_as_server",
    "model_rejected_server_fallback",
    "model_rejected_fail_closed",
    "provider_error_server_fallback",
    "provider_error_fail_closed",
    "model_not_eligible",
]


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _model_sha256(model: GroundedPlanProposal | QueryPlan | None) -> str | None:
    if model is None:
        return None
    return _canonical_sha256(model.model_dump(mode="json"))


def _reason_cluster(reason: str | None) -> str:
    if reason is None:
        return "none"
    patterns = (
        "proposal evidence span is not verbatim",
        "identity evidence does not contain",
        "identity evidence is negated or ambiguous",
        "identity proposal operator must be eq or in",
        "constraint lacks lexical grounding",
        "proposal omitted trusted constraints",
        "ranking field lacks grounding",
        "ranking evidence is negated or ambiguous",
        "ranking direction lacks grounding",
        "payload field lacks grounding",
        "payload field evidence is negated or ambiguous",
        "aggregation field lacks grounding",
        "aggregation function lacks grounding",
        "aggregation evidence is negated or ambiguous",
        "limit evidence is negated or ambiguous",
        "local model returned an invalid grounded plan",
        "identity does not resolve uniquely across datasets",
        "model intent differs from grounded route intent",
        "model family differs from explicit route family",
        "proposal reports unresolved conditions",
        "trusted linker found a blocked condition",
    )
    for pattern in patterns:
        if pattern in reason:
            return pattern
    return reason.split(":", maxsplit=1)[0][:160]


class GroundedPlanAuditCase(MetamorphicModel):
    id: str
    source_case_id: str
    axis: str
    coverage_family: ProductFamily
    question: str
    route_disposition: str
    route_reason_code: str
    route_intent: str
    routed_product_families: list[ProductFamily]
    eligible: bool
    server_plan_status: Literal["accepted", "blocked", "not_applicable"]
    server_plan_reason: str | None
    server_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    server_plan: QueryPlan | None
    provider_status: Literal["valid", "error", "not_called"]
    provider_error_type: str | None
    provider_error: str | None
    provider_latency_ms: float = Field(ge=0)
    proposal_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    proposal: GroundedPlanProposal | None
    gate_status: Literal["accepted", "rejected", "not_called"]
    gate_reason: str | None
    gate_reason_cluster: str
    gated_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    gated_plan: QueryPlan | None
    outcome: GroundedPlanOutcome


class GroundedPlanAuditSummary(MetamorphicModel):
    total: int = Field(ge=0)
    eligible: int = Field(ge=0)
    server_plan_accepted: int = Field(ge=0)
    provider_valid: int = Field(ge=0)
    provider_errors: int = Field(ge=0)
    gate_accepted: int = Field(ge=0)
    gate_rejected: int = Field(ge=0)
    model_rescues: int = Field(ge=0)
    model_supplements: int = Field(ge=0)
    model_same_as_server: int = Field(ge=0)
    fail_closed: int = Field(ge=0)
    outcome_counts: dict[str, int]
    gate_reason_counts: dict[str, int]
    family_gate_acceptance: dict[str, float | None]
    provider_latency_ms: dict[str, float]


class GroundedPlanAuditReport(MetamorphicModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: str
    generated_at_utc: str
    status: Literal["internal_development_not_blind"]
    provider: str
    model: str
    batch_id: str
    protocol_id: str
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mutation_batch_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_sha256_by_family: dict[str, str]
    summary: GroundedPlanAuditSummary
    cases: list[GroundedPlanAuditCase]
    interpretation_limits: list[str]


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * quantile))
    return round(ordered[index], 3)


class GroundedPlanAuditRunner:
    """Separate model proposal quality from downstream retrieval and answer quality."""

    def __init__(
        self,
        *,
        batch: MutationBatch,
        database_paths: Mapping[ProductFamily | str, str | Path],
        providers: Mapping[ProductFamily | str, GroundedPlanProvider],
        database_sha256_by_family: Mapping[str, str],
    ) -> None:
        self.batch = batch
        self.database_paths = {
            ProductFamily(family): Path(path) for family, path in database_paths.items()
        }
        self.providers = {ProductFamily(family): provider for family, provider in providers.items()}
        self.database_sha256_by_family = dict(database_sha256_by_family)
        self.router = IntentRouter()
        identity_cache = ProductIdentitySnapshotCache(max_entries=4)
        record_cache = RecordSnapshotCache(max_entries=4)
        self.compiler = ServerQueryPlanCompiler(
            self.database_paths,
            record_cache=record_cache,
            identity_cache=identity_cache,
        )
        self.gate = GroundedPlanGate(
            self.database_paths,
            identity_cache=identity_cache,
        )

    def run(self) -> GroundedPlanAuditReport:
        cases = [
            self._run_candidate(candidate)
            for candidate in self.batch.candidates
            if candidate.validation.passed
        ]
        first_provider = next(iter(self.providers.values()))
        return GroundedPlanAuditReport(
            report_id=f"{self.batch.protocol_id}-grounded-plan-audit",
            generated_at_utc=datetime.now(UTC).isoformat(),
            status="internal_development_not_blind",
            provider=first_provider.provider_name,
            model=first_provider.model_name,
            batch_id=self.batch.batch_id,
            protocol_id=self.batch.protocol_id,
            protocol_sha256=self.batch.protocol_sha256,
            mutation_batch_semantic_sha256=mutation_batch_semantic_sha256(self.batch),
            database_sha256_by_family=self.database_sha256_by_family,
            summary=self._summary(cases),
            cases=cases,
            interpretation_limits=[
                "공개 개발 정답에서 Qwen이 다시 만든 질문을 사용하므로 독립 blind 평가가 아님",
                "이 보고서는 모델 제안과 안전 게이트만 측정하며 검색·답변 정답률을 대신하지 않음",
                "gate accepted는 정답 보장이 아니라 모든 실행 항목에 원문 근거가 확인됐다는 뜻임",
                "Qwen은 내부 개발 진단용이며 공식 제출 허용 모델 또는 HyperCLOVA X 성능이 아님",
            ],
        )

    def _run_candidate(self, candidate: MutationCandidate) -> GroundedPlanAuditCase:
        decision = self.router.route(candidate.question, candidate.id)
        eligible = grounded_plan_is_eligible(decision)
        server_plan: QueryPlan | None = None
        server_reason: str | None = None
        if decision.disposition.value == "execute":
            try:
                server_plan = self.compiler.compile(decision)
                server_status: Literal["accepted", "blocked", "not_applicable"] = "accepted"
            except PlanCompilationBlockedError as error:
                server_status = "blocked"
                server_reason = str(error)
        else:
            server_status = "not_applicable"
            server_reason = decision.reason

        proposal: GroundedPlanProposal | None = None
        provider_error_type: str | None = None
        provider_error: str | None = None
        provider_latency_ms = 0.0
        gated_plan: QueryPlan | None = None
        gate_reason: str | None = None
        if not eligible:
            provider_status: Literal["valid", "error", "not_called"] = "not_called"
            gate_status: Literal["accepted", "rejected", "not_called"] = "not_called"
            outcome: GroundedPlanOutcome = "model_not_eligible"
        else:
            provider = self.providers[candidate.coverage_family]
            family_hint = (
                decision.draft.product_families[0]
                if len(decision.draft.product_families) == 1
                else None
            )
            started = time.perf_counter()
            try:
                proposal = provider.generate_grounded_plan(
                    candidate.question,
                    candidate.id,
                    family_hint,
                )
                provider_status = "valid"
            except Exception as error:
                provider_status = "error"
                provider_error_type = type(error).__name__
                provider_error = str(error)[:4000]
            provider_latency_ms = round((time.perf_counter() - started) * 1000, 3)

            if proposal is None:
                gate_status = "not_called"
                outcome = (
                    "provider_error_server_fallback"
                    if server_plan is not None
                    else "provider_error_fail_closed"
                )
            else:
                try:
                    gated_plan = self.gate.compile(
                        candidate.question,
                        decision,
                        proposal,
                        trusted_plan=server_plan,
                    )
                    gate_status = "accepted"
                except GroundedPlanRejectedError as error:
                    gate_status = "rejected"
                    gate_reason = str(error)
                if gated_plan is None:
                    outcome = (
                        "model_rejected_server_fallback"
                        if server_plan is not None
                        else "model_rejected_fail_closed"
                    )
                elif server_plan is None:
                    outcome = "model_rescue"
                elif _model_sha256(gated_plan) == _model_sha256(server_plan):
                    outcome = "model_same_as_server"
                else:
                    outcome = "model_supplement"

        return GroundedPlanAuditCase(
            id=candidate.id,
            source_case_id=candidate.source_case_id,
            axis=candidate.axis.value,
            coverage_family=candidate.coverage_family,
            question=candidate.question,
            route_disposition=decision.disposition.value,
            route_reason_code=decision.reason_code,
            route_intent=decision.draft.intent.value,
            routed_product_families=decision.draft.product_families,
            eligible=eligible,
            server_plan_status=server_status,
            server_plan_reason=server_reason,
            server_plan_sha256=_model_sha256(server_plan),
            server_plan=server_plan,
            provider_status=provider_status,
            provider_error_type=provider_error_type,
            provider_error=provider_error,
            provider_latency_ms=provider_latency_ms,
            proposal_sha256=_model_sha256(proposal),
            proposal=proposal,
            gate_status=gate_status,
            gate_reason=gate_reason,
            gate_reason_cluster=_reason_cluster(gate_reason or provider_error),
            gated_plan_sha256=_model_sha256(gated_plan),
            gated_plan=gated_plan,
            outcome=outcome,
        )

    @staticmethod
    def _summary(cases: list[GroundedPlanAuditCase]) -> GroundedPlanAuditSummary:
        outcomes = Counter(case.outcome for case in cases)
        reasons = Counter(
            case.gate_reason_cluster
            for case in cases
            if case.gate_status == "rejected" or case.provider_status == "error"
        )
        family_rates: dict[str, float | None] = {}
        for family in ProductFamily:
            eligible = [case for case in cases if case.coverage_family is family and case.eligible]
            accepted = sum(case.gate_status == "accepted" for case in eligible)
            family_rates[family.value] = (
                None if not eligible else round(accepted / len(eligible), 6)
            )
        latencies = [case.provider_latency_ms for case in cases if case.eligible]
        fail_closed = sum(case.outcome.endswith("fail_closed") for case in cases)
        return GroundedPlanAuditSummary(
            total=len(cases),
            eligible=sum(case.eligible for case in cases),
            server_plan_accepted=sum(case.server_plan_status == "accepted" for case in cases),
            provider_valid=sum(case.provider_status == "valid" for case in cases),
            provider_errors=sum(case.provider_status == "error" for case in cases),
            gate_accepted=sum(case.gate_status == "accepted" for case in cases),
            gate_rejected=sum(case.gate_status == "rejected" for case in cases),
            model_rescues=outcomes["model_rescue"],
            model_supplements=outcomes["model_supplement"],
            model_same_as_server=outcomes["model_same_as_server"],
            fail_closed=fail_closed,
            outcome_counts=dict(sorted(outcomes.items())),
            gate_reason_counts=dict(sorted(reasons.items())),
            family_gate_acceptance=family_rates,
            provider_latency_ms={
                "min": round(min(latencies), 3) if latencies else 0.0,
                "p50": _percentile(latencies, 0.5),
                "p95": _percentile(latencies, 0.95),
                "max": round(max(latencies), 3) if latencies else 0.0,
                "total": round(sum(latencies), 3),
            },
        )
