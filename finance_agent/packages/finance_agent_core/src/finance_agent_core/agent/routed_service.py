from __future__ import annotations

import contextvars
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
from finance_agent_core.agent.grounded_planning import (
    GroundedPlanGate,
    GroundedPlanProvider,
    GroundedPlanRejectedError,
    grounded_plan_is_eligible,
)
from finance_agent_core.agent.planning_policy import (
    AdaptiveShadowPlanningPolicy,
    PlanningDecision,
)
from finance_agent_core.agent.providers import HyperClovaXTimeoutError, QueryPlanProvider
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.agent.semantic_gate import SemanticCoverageDecision
from finance_agent_core.answering import (
    AnswerComposition,
    CrossFamilyAnswerVerifier,
    GroundedAnswerProvider,
    compose_grounded_answer,
)
from finance_agent_core.contracts import QueryPlan, RouteDecision, RouteDisposition
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RoutedExecutionError
from finance_agent_core.deadline import RequestDeadlineExceeded
from finance_agent_core.domain import (
    AggregateEvidence,
    ComparisonEvidence,
    DatabaseManifest,
    ProductEvidence,
    VerifiedSearch,
)
from finance_agent_core.execution import (
    AggregateResultVerifier,
    PlanAuthorityCode,
    PlanAuthorityError,
    PlanAuthorityGate,
    PlanCompilerKind,
    PlanExecutionBlockedError,
    ResultVerifier,
    SQLiteAggregateOracle,
    SQLiteOracle,
    ValidatedPlan,
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
from finance_agent_core.execution.authority import (
    require_manifest_binding,
    require_validated_plan,
    require_verifier_budget,
)
from finance_agent_core.execution.verifier_projection import (
    load_projected_verifier_records,
)
from finance_agent_core.release import ResolvedAgentRelease
from finance_agent_core.storage import (
    ProductIdentitySnapshotCache,
    RecordSnapshotCache,
    require_approved_database_paths,
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
            self.query_plan is not None
            or self.candidate_count is not None
            or self.products
            or self.aggregates
            or self.comparisons
            or self.warnings
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
        grounded_plan_provider: GroundedPlanProvider | None = None,
        answer_provider: GroundedAnswerProvider | None = None,
        hclx_planning_enabled: bool = False,
        allow_internal_disabled_dataset: bool = False,
        capability_execution_overrides: set[ProductFamily | str] | None = None,
        require_approved_databases: bool = False,
        release_guard: ResolvedAgentRelease | None = None,
        require_agent_release: bool = False,
        record_cache: RecordSnapshotCache | None = None,
        identity_cache: ProductIdentitySnapshotCache | None = None,
    ) -> None:
        if type(hclx_planning_enabled) is not bool:
            raise TypeError("hclx_planning_enabled must be a boolean")
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
        self.hclx_planning_enabled = hclx_planning_enabled
        self.router = router or IntentRouter(hclx_planning_enabled=hclx_planning_enabled)
        self.query_plan_provider = query_plan_provider
        self.grounded_plan_provider = grounded_plan_provider
        self.compiler = ServerQueryPlanCompiler(
            self.database_paths,
            record_cache=self.record_cache,
            identity_cache=self.identity_cache,
        )
        self.grounded_plan_gate = GroundedPlanGate(
            self.database_paths,
            identity_cache=self.identity_cache,
        )
        self.answer_provider = answer_provider
        self.allow_internal_disabled_dataset = allow_internal_disabled_dataset
        self.capability_execution_overrides = frozenset(
            ProductFamily(family) for family in capability_execution_overrides or set()
        )
        self.require_approved_databases = require_approved_databases
        if release_guard is not None and type(release_guard) is not ResolvedAgentRelease:
            raise TypeError("release_guard must be a ResolvedAgentRelease")
        if require_agent_release and release_guard is None:
            raise ValueError("public RoutedFinanceAgent requires a resolved Agent release")
        self.release_guard = release_guard
        self.require_agent_release = require_agent_release
        self.plan_authority_gate = PlanAuthorityGate(
            self.database_paths,
            require_approved_databases=require_approved_databases,
            allow_internal_disabled_dataset=allow_internal_disabled_dataset,
            capability_execution_overrides=self.capability_execution_overrides,
            release_guard=release_guard,
            require_agent_release=require_agent_release,
        )

    def _record_universe(self, database_path: Path, validated_plan: ValidatedPlan):
        require_validated_plan(validated_plan, database_path)
        if self._record_cache_enabled:
            snapshot = self.record_cache.get(database_path)
            require_manifest_binding(validated_plan, snapshot.manifest)
            require_verifier_budget(validated_plan, len(snapshot.records))
            require_validated_plan(validated_plan, database_path)
            return snapshot.records
        return load_projected_verifier_records(database_path, validated_plan)

    def answer(self, question: str, request_id: str) -> RoutedAgentResult:
        # Evaluation/production code lives in the digest-pinned read-only image.
        # Startup and readiness perform the expensive deep tree hash; request
        # boundaries recheck the detached immutable release files only.
        if self.release_guard is not None:
            self.release_guard.assert_request_current()
        if self.require_approved_databases:
            require_approved_database_paths(self.database_paths)
        try:
            return self._answer_once(question, request_id)
        finally:
            # Detect a path replacement that occurred after the pre-execution
            # approval check.  A changed inode/ctime invalidates the cached
            # approval and the result is discarded before it can be served.
            if self.require_approved_databases:
                require_approved_database_paths(self.database_paths)
            if self.release_guard is not None:
                self.release_guard.assert_request_current()

    def _answer_atomically(self, question: str, request_id: str) -> RoutedAgentResult:
        """Internal adapter seam: route and execute exactly once inside this service."""

        if self.release_guard is not None:
            self.release_guard.assert_request_current()
        if self.require_approved_databases:
            require_approved_database_paths(self.database_paths)
        try:
            result = self._answer_once(
                question,
                request_id,
                capture_execution_error=True,
            )
        except RoutedExecutionError as error:
            if self.require_approved_databases:
                try:
                    require_approved_database_paths(self.database_paths)
                except Exception as approval_error:  # noqa: BLE001 - replace stale result cause
                    raise RoutedExecutionError(error.decision, approval_error) from approval_error
            if self.release_guard is not None:
                try:
                    self.release_guard.assert_request_current()
                except Exception as release_error:  # noqa: BLE001 - replace stale result cause
                    raise RoutedExecutionError(error.decision, release_error) from release_error
            raise
        if self.require_approved_databases:
            try:
                require_approved_database_paths(self.database_paths)
            except Exception as error:  # noqa: BLE001 - retain the trusted routed scope
                raise RoutedExecutionError(result.decision, error) from error
        if self.release_guard is not None:
            try:
                self.release_guard.assert_request_current()
            except Exception as error:  # noqa: BLE001 - retain trusted routed scope
                raise RoutedExecutionError(result.decision, error) from error
        return result

    def _answer_once(
        self,
        question: str,
        request_id: str,
        *,
        capture_execution_error: bool = False,
    ) -> RoutedAgentResult:
        trace = self.router.route_with_planning(question, request_id)
        decision = trace.route_decision
        try:
            decision = self._resolve_exact_identity_family(decision)
            planning_decision = trace.planning_decision
            if decision != trace.route_decision:
                # Exact product identity is a server-owned DB resolution, not
                # a caller-provided RouteDecision. Recompute the shadow record
                # from the final route so CONTROL metadata cannot silently
                # accompany an executable plan.
                planning_decision = AdaptiveShadowPlanningPolicy(
                    hclx_planning_enabled=self.hclx_planning_enabled
                ).decide(
                    decision,
                    SemanticCoverageDecision(),
                )
            return self._answer_from_decision(decision, planning_decision)
        except Exception as error:  # noqa: BLE001 - private atomic error transport
            if capture_execution_error:
                raise RoutedExecutionError(decision, error) from error
            raise

    def _answer_from_decision(
        self,
        decision: RouteDecision,
        planning_decision: PlanningDecision,
    ) -> RoutedAgentResult:
        question = decision.draft.question
        request_id = decision.draft.request_id
        if (
            decision.disposition is RouteDisposition.EXECUTE
            and ProductFamily.FUND in decision.draft.product_families
            and not self._fund_execution_enabled()
        ):
            return self._control_result(
                decision,
                disposition=RouteDisposition.UNSUPPORTED,
                reason=(
                    "공모펀드 공식 실행은 승인 플래그가 잠겨 있어 현재 공개 경로에서 "
                    "처리할 수 없습니다."
                ),
            )
        if len(decision.draft.product_families) > 1:
            if decision.disposition is not RouteDisposition.EXECUTE:
                return self._control_result(decision)
            return self._answer_cross_family_search(decision, planning_decision)
        try:
            compiled = self._compile_with_optional_grounded_plan(
                question,
                decision,
                planning_decision,
            )
        except PlanCompilationBlockedError as error:
            return self._control_result(
                decision,
                disposition=RouteDisposition.CLARIFY,
                reason=str(error),
            )
        if compiled is None:
            if decision.disposition is not RouteDisposition.EXECUTE:
                return self._control_result(decision)
            return self._control_result(
                decision,
                disposition=RouteDisposition.CLARIFY,
                reason="질문의 실행 조건을 안전한 계획으로 확정하지 못했습니다.",
            )
        decision, plan, used_grounded_plan = compiled
        if used_grounded_plan:
            # Grounded planning is admitted only from an already executable
            # deterministic route. Recompute server-owned metadata from the
            # fully validated final decision while preserving the explicit
            # deployment-owned HCLX permission.
            planning_decision = AdaptiveShadowPlanningPolicy(
                hclx_planning_enabled=planning_decision.hclx_allowed
            ).decide(
                decision,
                SemanticCoverageDecision(),
            )

        # A blocked server plan must stop before any advisory provider call.
        # The same contract is checked again after provider admission below.
        try:
            self._require_execution_authority(decision, plan)
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

        if (
            self.query_plan_provider is not None
            and self.hclx_planning_enabled
            and planning_decision.hclx_allowed
            and not used_grounded_plan
            and plan.intent is Intent.SEARCH
        ):
            try:
                plan = self._provider_search_plan(decision, plan)
            except PlanCompilationBlockedError as error:
                return self._control_result(
                    decision,
                    disposition=RouteDisposition.CLARIFY,
                    reason=str(error),
                    plan=plan,
                )
            except (HyperClovaXTimeoutError, RequestDeadlineExceeded, TimeoutError):
                raise
            except Exception:
                # QueryPlan generation is advisory. Transport, schema, or
                # adapter failures retain the independently compiled server
                # plan; a model can never turn a valid deterministic request
                # into an infrastructure failure.
                pass

        # Preserve the existing user-facing CLARIFY/UNSUPPORTED classification
        # before resolving infrastructure. PlanAuthorityGate repeats these
        # checks on the final canonical proposal immediately before DB access.
        try:
            self._require_execution_authority(decision, plan)
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
        try:
            validated_plan = self.plan_authority_gate.validate_routed(
                plan,
                decision,
                planning_decision=planning_decision,
                compiler_kind=(
                    PlanCompilerKind.GROUNDED_PLAN_GATE
                    if used_grounded_plan
                    else PlanCompilerKind.SERVER_QUERY_PLAN
                ),
                proposal_provider_name=(
                    self.grounded_plan_provider.provider_name
                    if used_grounded_plan and self.grounded_plan_provider is not None
                    else None
                ),
                proposal_model_name=(
                    self.grounded_plan_provider.model_name
                    if used_grounded_plan and self.grounded_plan_provider is not None
                    else None
                ),
            )
            plan = validated_plan.canonical_plan
        except PlanAuthorityError as error:
            if error.code is not PlanAuthorityCode.EXECUTION_POLICY_BLOCKED:
                raise
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
        if plan.intent is Intent.AGGREGATE:
            universe = self._record_universe(database_path, validated_plan)
            executed_aggregation = SQLiteAggregateOracle(database_path).execute(validated_plan)
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
        executed = oracle.execute(validated_plan)
        universe = (
            None
            if plan.intent is Intent.COMPARE
            else self._record_universe(database_path, validated_plan)
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

    def _resolve_exact_identity_family(self, decision: RouteDecision) -> RouteDecision:
        if (
            decision.disposition is not RouteDisposition.CLARIFY
            or decision.reason_code != "ambiguous_product_family"
            or not decision.draft.product_mentions
        ):
            return decision
        resolved_families: set[ProductFamily] = set()
        for mention in decision.draft.product_mentions:
            matches: set[ProductFamily] = set()
            for family, path in self.database_paths.items():
                for record in self.identity_cache.get(path).records:
                    identities = (record.product_id, record.ticker, record.isin)
                    if any(
                        value is not None and value.casefold() == mention.casefold()
                        for value in identities
                    ):
                        matches.add(family)
            if len(matches) != 1:
                return decision
            resolved_families.update(matches)
        if len(resolved_families) != 1:
            return decision
        family = next(iter(resolved_families))
        capability = self.router.matrix.require(family, decision.draft.intent)
        if capability.status != "executable" or capability.query_plan_intent is None:
            return decision
        draft = decision.draft.model_copy(update={"product_families": [family]})
        return RouteDecision(
            draft=draft,
            disposition=RouteDisposition.EXECUTE,
            reason_code="exact_identity_family_resolved",
            reason="정확한 상품 식별자를 제공 데이터에서 유일한 상품군으로 확인",
            query_plan_intent=capability.query_plan_intent,
            capability_matrix_version=decision.capability_matrix_version,
        )

    def _compile_with_optional_grounded_plan(
        self,
        question: str,
        decision: RouteDecision,
        planning_decision: PlanningDecision,
    ) -> tuple[RouteDecision, QueryPlan, bool] | None:
        server_plan: QueryPlan | None = None
        server_error: PlanCompilationBlockedError | None = None
        if decision.disposition is RouteDisposition.EXECUTE:
            try:
                server_plan = self.compiler.compile(decision)
            except PlanCompilationBlockedError as error:
                server_error = error

        eligible = (
            self.grounded_plan_provider is not None
            and self.hclx_planning_enabled
            and planning_decision.hclx_allowed
            and grounded_plan_is_eligible(decision)
        )
        if eligible:
            assert self.grounded_plan_provider is not None
            family_hint = (
                decision.draft.product_families[0]
                if len(decision.draft.product_families) == 1
                else None
            )
            try:
                proposal = self.grounded_plan_provider.generate_grounded_plan(
                    question,
                    decision.draft.request_id,
                    family_hint,
                )
                grounded_plan = self.grounded_plan_gate.compile(
                    question,
                    decision,
                    proposal,
                    trusted_plan=server_plan,
                )
                grounded_decision = self._grounded_execution_decision(
                    decision,
                    grounded_plan,
                )
                return grounded_decision, grounded_plan, True
            except (HyperClovaXTimeoutError, RequestDeadlineExceeded, TimeoutError):
                raise
            except GroundedPlanRejectedError:
                pass
            except Exception:
                # A planning model is advisory only. Malformed output, transport
                # failure, or an adapter bug must never become an HTTP 500 or
                # acquire execution authority. Reuse the independently compiled
                # server plan when one exists; otherwise answer with the normal
                # fail-closed clarification path.
                pass
        if server_plan is None:
            if server_error is not None:
                raise server_error
            return None
        return decision, server_plan, False

    @staticmethod
    def _grounded_execution_decision(
        decision: RouteDecision,
        plan: QueryPlan,
    ) -> RouteDecision:
        if plan.intent is Intent.COMPARE:
            interaction_intent = InteractionIntent.COMPARE
        elif plan.intent is Intent.AGGREGATE:
            interaction_intent = InteractionIntent.AGGREGATE
        elif decision.draft.intent in {
            InteractionIntent.DETAIL,
            InteractionIntent.EXPLAIN,
        }:
            interaction_intent = decision.draft.intent
        else:
            interaction_intent = InteractionIntent.SEARCH
        mentions: list[str] = []
        for constraint in plan.constraints:
            if constraint.field not in {"product_id", "ticker", "isin"}:
                continue
            values = constraint.value if isinstance(constraint.value, list) else [constraint.value]
            mentions.extend(str(value) for value in values)
        draft = decision.draft.model_copy(
            update={
                "intent": interaction_intent,
                "product_families": plan.product_families,
                "product_mentions": list(dict.fromkeys(mentions)),
            }
        )
        return RouteDecision(
            draft=draft,
            disposition=RouteDisposition.EXECUTE,
            reason_code="grounded_model_plan_accepted",
            reason="모델 계획의 모든 실행 조건을 원문 근거와 서버 계약으로 검증",
            query_plan_intent=plan.intent,
            capability_matrix_version=decision.capability_matrix_version,
        )

    def _answer_cross_family_search(
        self,
        decision: RouteDecision,
        planning_decision: PlanningDecision,
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
        validated_searches: list[tuple[CompiledFamilySearch, ValidatedPlan]] = []
        try:
            # Authorize the complete ordered batch before starting any worker.
            # A failure in child N therefore cannot leave children 0..N-1
            # partially executed.
            for index, search in enumerate(searches):
                validated = self.plan_authority_gate.validate_routed(
                    search.plan,
                    decision,
                    planning_decision=planning_decision,
                    cross_family_index=index,
                    cross_family_total=len(searches),
                )
                validated_searches.append((search, validated))
        except PlanAuthorityError as error:
            if error.code is not PlanAuthorityCode.EXECUTION_POLICY_BLOCKED:
                raise
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
            futures = [
                executor.submit(
                    contextvars.copy_context().run,
                    self._execute_family_search,
                    search,
                    validated,
                )
                for search, validated in validated_searches
            ]
            executions = [future.result() for future in futures]

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
        validated_plan: ValidatedPlan,
    ) -> _ExecutedFamilySearch:
        plan = validated_plan.canonical_plan
        family = plan.product_families[0]
        database_path = self.database_paths[family]
        executed = SQLiteOracle(database_path).execute(validated_plan)
        universe = self._record_universe(database_path, validated_plan)
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
        # Equality is only an admission check. Keep the server-owned object as
        # the proposal that reaches PlanAuthorityGate so a provider instance is
        # never substituted after deterministic validation.
        return server_plan

    def _require_execution(self, plan: QueryPlan) -> None:
        uses_approved_override = bool(plan.product_families) and set(
            plan.product_families
        ).issubset(
            self.capability_execution_overrides,
        )
        use_internal_contract = self.allow_internal_disabled_dataset or uses_approved_override
        if plan.intent is Intent.AGGREGATE:
            if use_internal_contract:
                require_internal_evaluation_aggregation(plan)
            else:
                require_executable_aggregation(plan)
        elif plan.intent is Intent.COMPARE:
            if use_internal_contract:
                require_internal_evaluation_comparison(plan)
            else:
                require_executable_comparison(plan)
        elif use_internal_contract:
            require_internal_evaluation_search(plan)
        else:
            require_executable_search(plan)

    def _fund_execution_enabled(self) -> bool:
        return self.allow_internal_disabled_dataset or (
            ProductFamily.FUND in self.capability_execution_overrides
        )

    @staticmethod
    def _require_execution_authority(decision: RouteDecision, plan: QueryPlan) -> None:
        """Recheck server-owned authority immediately before an Oracle boundary."""

        if decision.disposition is not RouteDisposition.EXECUTE:
            raise PlanExecutionBlockedError("control disposition cannot reach an Oracle")
        if decision.query_plan_intent is not plan.intent:
            raise PlanExecutionBlockedError("route and QueryPlan intents do not match")
        if plan.ambiguities or plan.unsupported_conditions:
            raise PlanExecutionBlockedError(
                "ambiguous or unsupported QueryPlan cannot reach an Oracle"
            )

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
        control_intent = (
            InteractionIntent.CLARIFY
            if actual_disposition is RouteDisposition.CLARIFY
            else InteractionIntent.UNSUPPORTED
        )
        control_reason = reason or decision.reason
        if decision.disposition is not actual_disposition or decision.query_plan_intent is not None:
            decision = RouteDecision(
                draft=decision.draft.model_copy(update={"intent": control_intent}),
                disposition=actual_disposition,
                reason_code=f"{control_intent.value}_execution_blocked",
                reason=control_reason,
                query_plan_intent=None,
                capability_matrix_version=decision.capability_matrix_version,
            )
        return RoutedAgentResult(
            request_id=decision.draft.request_id,
            status=status,
            decision=decision,
            answer=_control_answer(actual_disposition, control_reason),
            # A control response never exports a partially compiled plan.  This
            # keeps execution authority and evidence unambiguously absent at
            # every public boundary, even when compilation found a later issue.
            query_plan=None,
            candidate_count=None,
            products=[],
            aggregates=[],
            comparisons=[],
            warnings=[],
            source_manifest=None,
            answer_composition=None,
        )
