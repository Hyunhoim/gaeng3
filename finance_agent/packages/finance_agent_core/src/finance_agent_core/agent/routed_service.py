from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.compiler import (
    PlanCompilationBlockedError,
    ServerQueryPlanCompiler,
)
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.answering import (
    AnswerComposition,
    GroundedAnswerProvider,
    compose_grounded_answer,
)
from finance_agent_core.contracts import QueryPlan, RouteDecision, RouteDisposition
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.domain import AggregateEvidence, DatabaseManifest, ProductEvidence
from finance_agent_core.execution import (
    AggregateResultVerifier,
    PlanExecutionBlockedError,
    ResultVerifier,
    SQLiteAggregateOracle,
    SQLiteOracle,
    build_aggregate_evidence,
    build_fund_comparison,
    build_product_evidence,
    render_blocked_plan,
    render_verified_aggregation,
    render_verified_search,
    require_executable_aggregation,
    require_executable_comparison,
    require_executable_search,
    require_internal_evaluation_aggregation,
    require_internal_evaluation_comparison,
    require_internal_evaluation_search,
)
from finance_agent_core.storage import connect_read_only, load_all_records


class RoutedServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoutedAgentResult(RoutedServiceModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    status: Literal["executed", "clarify", "unsupported"]
    decision: RouteDecision
    answer: str
    query_plan: QueryPlan | None
    candidate_count: int | None
    products: list[ProductEvidence]
    aggregates: list[AggregateEvidence] = Field(default_factory=list)
    warnings: list[str]
    source_manifest: DatabaseManifest | None
    answer_composition: AnswerComposition | None

    @model_validator(mode="after")
    def validate_result_state(self) -> RoutedAgentResult:
        if self.request_id != self.decision.draft.request_id:
            raise ValueError("result and route request IDs differ")
        if self.status == "executed":
            if (
                self.query_plan is None
                or self.candidate_count is None
                or self.source_manifest is None
            ):
                raise ValueError("executed result requires plan, count, and source manifest")
        elif (
            self.candidate_count is not None
            or self.products
            or self.aggregates
            or self.source_manifest is not None
            or self.answer_composition is not None
        ):
            raise ValueError("control result must not contain executed evidence")
        return self


def _control_answer(disposition: RouteDisposition, reason: str) -> str:
    if disposition is RouteDisposition.CLARIFY:
        return (
            f"질문을 실행하지 않았습니다. {reason} "
            "확인할 상품군·상품 식별자·수치 기준을 구체적으로 알려주세요."
        )
    return (
        f"요청을 실행하지 않았습니다. {reason} "
        "제공 데이터에 근거한 조회·상세 설명 범위로 질문을 바꿔주세요."
    )


class RoutedFinanceAgent:
    """Shared fail-closed path for routing, compilation, Oracle, evidence and answer."""

    def __init__(
        self,
        database_paths: dict[ProductFamily | str, str | Path],
        *,
        router: IntentRouter | None = None,
        answer_provider: GroundedAnswerProvider | None = None,
        allow_internal_disabled_dataset: bool = False,
    ) -> None:
        self.database_paths = {
            ProductFamily(key): Path(value) for key, value in database_paths.items()
        }
        self.router = router or IntentRouter()
        self.compiler = ServerQueryPlanCompiler(self.database_paths)
        self.answer_provider = answer_provider
        self.allow_internal_disabled_dataset = allow_internal_disabled_dataset

    def answer(self, question: str, request_id: str) -> RoutedAgentResult:
        decision = self.router.route(question, request_id)
        if decision.disposition is not RouteDisposition.EXECUTE:
            return self._control_result(decision)
        try:
            plan = self.compiler.compile(decision)
        except PlanCompilationBlockedError as error:
            return self._control_result(
                decision,
                disposition=RouteDisposition.CLARIFY,
                reason=str(error),
            )

        try:
            self._require_execution(plan)
        except PlanExecutionBlockedError as error:
            disposition = (
                RouteDisposition.UNSUPPORTED
                if plan.unsupported_conditions
                else RouteDisposition.CLARIFY
            )
            answer = render_blocked_plan(plan, plan.product_families[0].value)
            return self._control_result(
                decision,
                disposition=disposition,
                reason=f"{answer} {error}",
                plan=plan,
            )

        family = plan.product_families[0]
        try:
            database_path = self.database_paths[family]
        except KeyError:
            return self._control_result(
                decision,
                disposition=RouteDisposition.UNSUPPORTED,
                reason=f"{family.value} database path is not configured",
                plan=plan,
            )
        with connect_read_only(database_path) as connection:
            universe = load_all_records(connection)
        if plan.intent is Intent.AGGREGATE:
            executed_aggregation = SQLiteAggregateOracle(database_path).execute(plan)
            verified_aggregation = AggregateResultVerifier().verify(
                plan,
                executed_aggregation,
                universe,
            )
            aggregates = build_aggregate_evidence(plan, verified_aggregation)
            answer, warnings = render_verified_aggregation(
                plan,
                verified_aggregation,
                aggregates,
            )
            return RoutedAgentResult(
                request_id=request_id,
                status="executed",
                decision=decision,
                answer=answer,
                query_plan=plan,
                candidate_count=verified_aggregation.candidate_count,
                products=[],
                aggregates=aggregates,
                warnings=warnings,
                source_manifest=verified_aggregation.manifest,
                answer_composition=None,
            )
        oracle = SQLiteOracle(database_path)
        executed = oracle.execute(plan)
        verified = ResultVerifier().verify(plan, executed, universe)
        products = build_product_evidence(plan, verified)
        if plan.intent is Intent.COMPARE:
            comparison = build_fund_comparison(plan, verified, products)
            verified = comparison.verified
            products = list(comparison.products)
        answer, warnings = render_verified_search(plan, verified, products)
        composition: AnswerComposition | None = None
        if self.answer_provider is not None:
            composition = compose_grounded_answer(
                question=question,
                plan=plan,
                verified=verified,
                products=products,
                provider=self.answer_provider,
            )
            answer = composition.answer
        return RoutedAgentResult(
            request_id=request_id,
            status="executed",
            decision=decision,
            answer=answer,
            query_plan=plan,
            candidate_count=verified.candidate_count,
            products=products,
            aggregates=[],
            warnings=warnings,
            source_manifest=verified.manifest,
            answer_composition=composition,
        )

    def _require_execution(self, plan: QueryPlan) -> None:
        if plan.intent is Intent.AGGREGATE:
            if self.allow_internal_disabled_dataset:
                require_internal_evaluation_aggregation(plan)
            else:
                require_executable_aggregation(plan)
        elif plan.intent is Intent.COMPARE:
            if self.allow_internal_disabled_dataset:
                require_internal_evaluation_comparison(plan)
            else:
                require_executable_comparison(plan)
        elif self.allow_internal_disabled_dataset:
            require_internal_evaluation_search(plan)
        else:
            require_executable_search(plan)

    def _control_result(
        self,
        decision: RouteDecision,
        *,
        disposition: RouteDisposition | None = None,
        reason: str | None = None,
        plan: QueryPlan | None = None,
    ) -> RoutedAgentResult:
        actual_disposition = disposition or decision.disposition
        status: Literal["clarify", "unsupported"] = (
            "clarify" if actual_disposition is RouteDisposition.CLARIFY else "unsupported"
        )
        return RoutedAgentResult(
            request_id=decision.draft.request_id,
            status=status,
            decision=decision,
            answer=_control_answer(actual_disposition, reason or decision.reason),
            query_plan=plan,
            candidate_count=None,
            products=[],
            aggregates=[],
            warnings=[],
            source_manifest=None,
            answer_composition=None,
        )
