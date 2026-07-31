from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.compiler import (
    CompiledFamilySearch,
    PlanCompilationBlockedError,
    ServerQueryPlanCompiler,
)
from finance_agent_core.agent.providers import QueryPlanProvider
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.answering import (
    AnswerComposition,
    CrossFamilyAnswerVerifier,
    GroundedAnswerProvider,
    compose_grounded_answer,
)
from finance_agent_core.contracts import QueryPlan, RouteDecision, RouteDisposition
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.domain import (
    AggregateEvidence,
    ComparisonEvidence,
    DatabaseManifest,
    ProductEvidence,
    VerifiedSearch,
)
from finance_agent_core.execution import (
    AggregateResultVerifier,
    PlanExecutionBlockedError,
    ResultVerifier,
    SQLiteAggregateOracle,
    SQLiteOracle,
    build_aggregate_evidence,
    build_comparison_evidence,
    build_product_comparison,
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
from finance_agent_core.execution.verifier_projection import (
    load_projected_verifier_records,
)
from finance_agent_core.storage import (
    ProductIdentitySnapshotCache,
    RecordSnapshotCache,
)


class RoutedServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FamilySearchResult(RoutedServiceModel):
    product_family: ProductFamily
    status: Literal["success", "not_found"]
    answer: str
    query_plan: QueryPlan
    candidate_count: int = Field(ge=0)
    products: list[ProductEvidence]
    warnings: list[str]
    source_manifest: DatabaseManifest

    @model_validator(mode="after")
    def validate_family_result(self) -> FamilySearchResult:
        if self.query_plan.intent is not Intent.SEARCH:
            raise ValueError("family search result requires a SEARCH QueryPlan")
        if self.query_plan.product_families != [self.product_family]:
            raise ValueError("family search result and QueryPlan family differ")
        if (self.status == "not_found") != (self.candidate_count == 0):
            raise ValueError("family search status and candidate count disagree")
        if self.status == "not_found" and self.products:
            raise ValueError("not_found family search cannot contain products")
        return self


@dataclass(frozen=True)
class _ExecutedFamilySearch:
    grounded_question: str
    result: FamilySearchResult
    verified: VerifiedSearch


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
    comparisons: list[ComparisonEvidence] = Field(default_factory=list)
    warnings: list[str]
    source_manifest: DatabaseManifest | None
    answer_composition: AnswerComposition | None
    family_searches: list[FamilySearchResult] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_result_state(self) -> RoutedAgentResult:
        if self.request_id != self.decision.draft.request_id:
            raise ValueError("result and route request IDs differ")
        if self.status == "executed":
            if self.family_searches:
                if len(self.family_searches) < 2:
                    raise ValueError("multi-family result requires at least two family searches")
                if (
                    self.query_plan is not None
                    or self.source_manifest is not None
                    or self.aggregates
                    or self.comparisons
                ):
                    raise ValueError(
                        "multi-family SEARCH result must keep plans and manifests per family"
                    )
                if self.candidate_count != sum(
                    item.candidate_count for item in self.family_searches
                ):
                    raise ValueError("multi-family candidate count must equal the family sum")
                if self.products != [
                    product for item in self.family_searches for product in item.products
                ]:
                    raise ValueError("multi-family products must preserve the family result order")
                if [item.product_family for item in self.family_searches] != (
                    self.decision.draft.product_families
                ):
                    raise ValueError("multi-family result order must match the routed family order")
                if (
                    self.answer_composition is not None
                    and self.answer_composition.answer != self.answer
                ):
                    raise ValueError("multi-family composition and served answer differ")
            elif (
                self.query_plan is None
                or self.candidate_count is None
                or self.source_manifest is None
            ):
                raise ValueError("executed result requires plan, count, and source manifest")
        elif (
            self.candidate_count is not None
            or self.products
            or self.aggregates
            or self.comparisons
            or self.source_manifest is not None
            or self.answer_composition is not None
            or self.family_searches
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


_FAMILY_LABELS = {
    ProductFamily.BOND: "국내채권",
    ProductFamily.DOMESTIC_ETP: "국내 ETP",
    ProductFamily.OVERSEAS_ETP: "해외 ETP",
    ProductFamily.FUND: "공모펀드",
}
_CROSS_FAMILY_SAFETY_NOTICE = (
    "상품군별로 독립 검색했으며, 상품군 간 수치의 직접 비교·합산·우열 판단은 수행하지 않았습니다."
)


def _compile_cross_family_answer(
    family_searches: list[FamilySearchResult],
) -> str:
    sections = [
        f"[{_FAMILY_LABELS[item.product_family]}]\n{item.answer}" for item in family_searches
    ]
    return "\n\n".join([*sections, _CROSS_FAMILY_SAFETY_NOTICE])


class RoutedFinanceAgent:
    """Shared fail-closed path for routing, compilation, Oracle, evidence and answer."""

    def __init__(
        self,
        database_paths: dict[ProductFamily | str, str | Path],
        *,
        router: IntentRouter | None = None,
        query_plan_provider: QueryPlanProvider | None = None,
        answer_provider: GroundedAnswerProvider | None = None,
        allow_internal_disabled_dataset: bool = False,
        record_cache: RecordSnapshotCache | None = None,
        identity_cache: ProductIdentitySnapshotCache | None = None,
    ) -> None:
        self.database_paths = {
            ProductFamily(key): Path(value) for key, value in database_paths.items()
        }
        self.record_cache = record_cache or RecordSnapshotCache(
            max_entries=max(1, len(self.database_paths))
        )
        self._record_cache_enabled = record_cache is not None
        self.identity_cache = identity_cache or ProductIdentitySnapshotCache(
            max_entries=max(1, len(self.database_paths))
        )
        self.router = router or IntentRouter()
        self.query_plan_provider = query_plan_provider
        self.compiler = ServerQueryPlanCompiler(
            self.database_paths,
            record_cache=self.record_cache,
            identity_cache=self.identity_cache,
        )
        self.answer_provider = answer_provider
        self.allow_internal_disabled_dataset = allow_internal_disabled_dataset

    def _record_universe(self, database_path: Path, plan: QueryPlan):
        if self._record_cache_enabled:
            return self.record_cache.get(database_path).records
        return load_projected_verifier_records(database_path, plan)

    def answer(self, question: str, request_id: str) -> RoutedAgentResult:
        decision = self.router.route(question, request_id)
        if decision.disposition is not RouteDisposition.EXECUTE:
            return self._control_result(decision)
        if len(decision.draft.product_families) > 1:
            return self._answer_cross_family_search(decision)
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
        if self.query_plan_provider is not None and plan.intent is Intent.SEARCH:
            try:
                plan = self._provider_search_plan(decision, plan)
            except PlanCompilationBlockedError as error:
                return self._control_result(
                    decision,
                    disposition=RouteDisposition.CLARIFY,
                    reason=str(error),
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
        if plan.intent is Intent.AGGREGATE:
            universe = self._record_universe(database_path, plan)
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
                comparisons=[],
                warnings=warnings,
                source_manifest=verified_aggregation.manifest,
                answer_composition=None,
            )
        oracle = SQLiteOracle(database_path)
        executed = oracle.execute(plan)
        universe = (
            None if plan.intent is Intent.COMPARE else self._record_universe(database_path, plan)
        )
        verified = ResultVerifier().verify(plan, executed, universe)
        products = build_product_evidence(plan, verified)
        comparisons: list[ComparisonEvidence] = []
        if plan.intent is Intent.COMPARE:
            comparison = build_product_comparison(plan, verified, products)
            verified = comparison.verified
            products = list(comparison.products)
            comparisons = build_comparison_evidence(comparison)
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
            comparisons=comparisons,
            warnings=warnings,
            source_manifest=verified.manifest,
            answer_composition=composition,
        )

    def _answer_cross_family_search(
        self,
        decision: RouteDecision,
    ) -> RoutedAgentResult:
        try:
            searches = self.compiler.compile_family_searches(decision)
        except PlanCompilationBlockedError as error:
            return self._control_result(
                decision,
                disposition=RouteDisposition.CLARIFY,
                reason=str(error),
            )
        plans = [search.plan for search in searches]
        for plan in plans:
            family = plan.product_families[0]
            if family not in self.database_paths:
                return self._control_result(
                    decision,
                    disposition=RouteDisposition.UNSUPPORTED,
                    reason=f"{family.value} database path is not configured",
                )
        try:
            for plan in plans:
                self._require_execution(plan)
        except PlanExecutionBlockedError as error:
            disposition = (
                RouteDisposition.UNSUPPORTED
                if any(plan.unsupported_conditions for plan in plans)
                else RouteDisposition.CLARIFY
            )
            return self._control_result(
                decision,
                disposition=disposition,
                reason=str(error),
            )

        with ThreadPoolExecutor(max_workers=len(searches)) as executor:
            executions = list(executor.map(self._execute_family_search, searches))

        deterministic_searches = [execution.result for execution in executions]
        family_searches = deterministic_searches
        answer_composition: AnswerComposition | None = None
        if self.answer_provider is not None:
            family_searches, answer_composition = self._compose_cross_family_grounded_answer(
                executions
            )

        products = [
            product for family_search in family_searches for product in family_search.products
        ]
        candidate_count = sum(family_search.candidate_count for family_search in family_searches)
        warnings = [
            _CROSS_FAMILY_SAFETY_NOTICE,
            *(
                f"{item.product_family.value}: {warning}"
                for item in family_searches
                for warning in item.warnings
            ),
        ]
        if self.query_plan_provider is not None:
            warnings.append(
                "교차 상품군 SEARCH는 QueryPlan 모델을 호출하지 않고 "
                "서버가 상품군별 실행 계획을 확정했습니다."
            )
        if answer_composition is not None and answer_composition.mode == "deterministic_fallback":
            warnings.append(
                "상품군별 생성 또는 교차 답변 검증이 실패해 전체 답변을 "
                "결정론적 결과로 교체했습니다."
            )
        return RoutedAgentResult(
            request_id=decision.draft.request_id,
            status="executed",
            decision=decision,
            answer=_compile_cross_family_answer(family_searches),
            query_plan=None,
            candidate_count=candidate_count,
            products=products,
            aggregates=[],
            comparisons=[],
            warnings=warnings,
            source_manifest=None,
            answer_composition=answer_composition,
            family_searches=family_searches,
        )

    def _execute_family_search(
        self,
        search: CompiledFamilySearch,
    ) -> _ExecutedFamilySearch:
        plan = search.plan
        family = plan.product_families[0]
        database_path = self.database_paths[family]
        executed = SQLiteOracle(database_path).execute(plan)
        universe = self._record_universe(database_path, plan)
        verified = ResultVerifier().verify(plan, executed, universe)
        products = build_product_evidence(plan, verified)
        answer, warnings = render_verified_search(plan, verified, products)
        return _ExecutedFamilySearch(
            grounded_question=search.grounded_question,
            result=FamilySearchResult(
                product_family=family,
                status="not_found" if verified.candidate_count == 0 else "success",
                answer=answer,
                query_plan=plan,
                candidate_count=verified.candidate_count,
                products=products,
                warnings=warnings,
                source_manifest=verified.manifest,
            ),
            verified=verified,
        )

    def _compose_cross_family_grounded_answer(
        self,
        executions: list[_ExecutedFamilySearch],
    ) -> tuple[list[FamilySearchResult], AnswerComposition]:
        if self.answer_provider is None:
            raise RuntimeError("cross-family grounded composition requires an answer provider")

        family_compositions: list[tuple[ProductFamily, AnswerComposition]] = []
        generated_searches: list[FamilySearchResult] = []
        for execution in executions:
            result = execution.result
            composition = compose_grounded_answer(
                question=execution.grounded_question,
                plan=result.query_plan,
                verified=execution.verified,
                products=result.products,
                provider=self.answer_provider,
            )
            family_compositions.append((result.product_family, composition))
            generated_searches.append(result.model_copy(update={"answer": composition.answer}))

        candidate_answer = _compile_cross_family_answer(generated_searches)
        verification = CrossFamilyAnswerVerifier().verify(
            family_compositions=family_compositions,
            family_answers=[(item.product_family, item.answer) for item in generated_searches],
            answer=candidate_answer,
            safety_notice=_CROSS_FAMILY_SAFETY_NOTICE,
        )
        latency_ms = round(
            sum(composition.generation_latency_ms for _, composition in family_compositions),
            3,
        )
        if verification.passed:
            mode: Literal["llm_grounded", "deterministic"] = (
                "llm_grounded"
                if any(composition.mode == "llm_grounded" for _, composition in family_compositions)
                else "deterministic"
            )
            return generated_searches, AnswerComposition(
                mode=mode,
                answer=candidate_answer,
                model=self.answer_provider.model_name,
                generation_latency_ms=latency_ms,
                draft=None,
                verification=verification,
            )

        deterministic_searches = [execution.result for execution in executions]
        deterministic_answer = _compile_cross_family_answer(deterministic_searches)
        return deterministic_searches, AnswerComposition(
            mode="deterministic_fallback",
            answer=deterministic_answer,
            model=self.answer_provider.model_name,
            generation_latency_ms=latency_ms,
            draft=None,
            verification=verification,
        )

    def _provider_search_plan(
        self,
        decision: RouteDecision,
        server_plan: QueryPlan,
    ) -> QueryPlan:
        if self.query_plan_provider is None:
            return server_plan
        provider_plan = self.query_plan_provider.generate_query_plan(
            decision.draft.question,
            decision.draft.request_id,
        )
        if provider_plan != server_plan:
            raise PlanCompilationBlockedError(
                "model QueryPlan differs from the server-compiled execution contract"
            )
        return provider_plan

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
            comparisons=[],
            warnings=[],
            source_manifest=None,
            answer_composition=None,
        )
