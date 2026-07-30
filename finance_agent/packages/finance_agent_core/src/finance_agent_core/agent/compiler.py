from __future__ import annotations

from pathlib import Path

from finance_agent_core.agent.aggregate_parser import (
    AggregatePlanParseError,
    compile_aggregate_plan,
)
from finance_agent_core.agent.fund_comparison_parser import (
    ResolvedFundComparisonPlanProvider,
    RuleFundComparisonDraftProvider,
)
from finance_agent_core.agent.fund_resolver import FundProductResolver
from finance_agent_core.agent.linker import canonicalize_query_plan_payload
from finance_agent_core.agent.product_comparison import (
    ProductComparisonParseError,
    compile_product_comparison_plan,
)
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    RouteDecision,
    RouteDisposition,
)
from finance_agent_core.storage import (
    ProductIdentitySnapshotCache,
    RecordSnapshotCache,
)


class PlanCompilationBlockedError(ValueError):
    """Raised when a routed draft cannot be compiled without guessing."""


class ServerQueryPlanCompiler:
    """Compile a minimal route draft into the server-owned QueryPlan contract."""

    def __init__(
        self,
        database_paths: dict[ProductFamily | str, str | Path],
        *,
        record_cache: RecordSnapshotCache | None = None,
        identity_cache: ProductIdentitySnapshotCache | None = None,
    ) -> None:
        self.database_paths = {
            ProductFamily(key): Path(value) for key, value in database_paths.items()
        }
        self.record_cache = record_cache or RecordSnapshotCache()
        self.identity_cache = identity_cache or ProductIdentitySnapshotCache()

    def compile(self, decision: RouteDecision) -> QueryPlan:
        if decision.disposition is not RouteDisposition.EXECUTE:
            raise PlanCompilationBlockedError("control routes cannot be compiled")
        if decision.query_plan_intent is Intent.SEARCH:
            return self._compile_search_lowering(decision)
        if decision.query_plan_intent is Intent.COMPARE:
            return self._compile_comparison(decision)
        if decision.query_plan_intent is Intent.AGGREGATE:
            return self._compile_aggregate(decision)
        raise PlanCompilationBlockedError(
            f"no server compiler for QueryPlan intent: {decision.query_plan_intent}"
        )

    def _compile_search_lowering(self, decision: RouteDecision) -> QueryPlan:
        family = decision.draft.product_families[0]
        payload = canonicalize_query_plan_payload(
            decision.draft.question,
            {
                "question_id": decision.draft.request_id,
                "product_families": [family.value],
            },
        )
        if decision.draft.requested_limit is not None:
            payload["limit"] = decision.draft.requested_limit
        if decision.draft.intent in {
            InteractionIntent.DETAIL,
            InteractionIntent.EXPLAIN,
        }:
            identity_constraints = [
                constraint
                for constraint in payload["constraints"]
                if constraint["field"] in {"product_id", "ticker", "isin"}
                and constraint["operator"] == "eq"
            ]
            if len(identity_constraints) != 1:
                raise PlanCompilationBlockedError(
                    "detail or explain requires one server-linked exact product identity"
                )
            payload["limit"] = 1
            payload["ranking"] = []
        plan = QueryPlan.model_validate(payload)
        if plan.product_families != [family]:
            raise PlanCompilationBlockedError("compiler changed the routed product family")
        return plan

    def _compile_comparison(self, decision: RouteDecision) -> QueryPlan:
        family = decision.draft.product_families[0]
        try:
            database_path = self.database_paths[family]
        except KeyError as error:
            raise PlanCompilationBlockedError(
                f"{family.value} comparison database path is not configured"
            ) from error
        identities = self.identity_cache.get(database_path).records
        if family is ProductFamily.FUND:
            provider = ResolvedFundComparisonPlanProvider(
                RuleFundComparisonDraftProvider(),
                FundProductResolver(identities),
            )
            plan = provider.generate_query_plan(
                decision.draft.question,
                decision.draft.request_id,
            )
            if plan.intent is not Intent.COMPARE:
                raise PlanCompilationBlockedError("fund comparison compiler changed the intent")
            return plan
        try:
            return compile_product_comparison_plan(
                question=decision.draft.question,
                question_id=decision.draft.request_id,
                family=family,
                mentions=decision.draft.product_mentions,
                records=identities,
            )
        except (ProductComparisonParseError, ValueError) as error:
            raise PlanCompilationBlockedError(str(error)) from error

    def _compile_aggregate(self, decision: RouteDecision) -> QueryPlan:
        family = decision.draft.product_families[0]
        base_payload = canonicalize_query_plan_payload(
            decision.draft.question,
            {
                "question_id": decision.draft.request_id,
                "product_families": [family.value],
            },
        )
        try:
            return compile_aggregate_plan(
                question=decision.draft.question,
                question_id=decision.draft.request_id,
                family=family,
                base_payload=base_payload,
                requested_limit=decision.draft.requested_limit,
            )
        except (AggregatePlanParseError, ValueError) as error:
            raise PlanCompilationBlockedError(str(error)) from error
