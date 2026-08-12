"""Evaluation-only adapter for the current routed Agent service path."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.agent import routed_service as routed_service_module
from finance_agent_core.contracts.queryplan import ProductFamily, QueryPlan
from finance_agent_core.evaluation.safety_blind import (
    APPROVED_FAMILIES,
    ApprovedUniverseFingerprint,
    ExpectedConstraint,
    ExpectedRanking,
    PublicSafetyBlindCase,
)
from finance_agent_core.evaluation.safety_blind_runner import (
    ExecutionSpies,
    SafetyBlindObservation,
)

DATABASES_ENV = "FINANCE_SAFETY_BLIND_DATABASES"


def _database_paths_from_environment() -> dict[ProductFamily, Path]:
    raw = os.environ.get(DATABASES_ENV)
    if raw is None:
        raise RuntimeError(f"{DATABASES_ENV} is required")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{DATABASES_ENV} must contain a JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != APPROVED_FAMILIES:
        raise RuntimeError(f"{DATABASES_ENV} must map the four approved families")
    paths = {ProductFamily(family): Path(str(path)) for family, path in payload.items()}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"approved database paths do not exist: {missing}")
    return paths


def _plan_observation(plan: QueryPlan | None) -> dict[str, Any]:
    if plan is None:
        return {
            "query_plan_present": False,
            "query_plan_intent": None,
            "product_families": [],
            "constraints": [],
            "ranking": [],
            "limit": None,
            "aggregate_functions": [],
        }
    return {
        "query_plan_present": True,
        "query_plan_intent": plan.intent.value,
        "product_families": [family.value for family in plan.product_families],
        "constraints": [
            ExpectedConstraint.model_validate(item.model_dump(mode="json"))
            for item in plan.constraints
        ],
        "ranking": [
            ExpectedRanking.model_validate(item.model_dump(mode="json")) for item in plan.ranking
        ],
        "limit": plan.limit,
        "aggregate_functions": [
            aggregation.function.value for aggregation in plan.intent_payload.aggregations
        ],
    }


def _build_evaluation_service() -> RoutedFinanceAgent:
    """Construct the production service with the evaluation-only approved fund path enabled."""

    return RoutedFinanceAgent(
        _database_paths_from_environment(),
        capability_execution_overrides={ProductFamily.FUND},
    )


class CurrentCoreSafetyBlindAdapter:
    """Run the sealed prompts through routing, compilation, Oracle and evidence."""

    name = "current-routed-finance-agent"
    scope = "full-routed-core-with-approved-fund-override-and-queryplan-provider-oracle-spies"

    def run(
        self,
        case: PublicSafetyBlindCase,
        universe: ApprovedUniverseFingerprint,
        spies: ExecutionSpies,
    ) -> SafetyBlindObservation:
        if case.context.release_id != universe.release_id:
            raise RuntimeError("case and approved universe release ids differ")
        service = _build_evaluation_service()

        # Each case runs in its own subprocess, so these evaluation-only patches
        # cannot leak into another request. Installation marks each boundary;
        # calls are recorded only if execution reaches it.
        service.compiler.compile = spies.wrap_query_plan_callable(
            "server_compiler.compile",
            service.compiler.compile,
        )
        service.compiler.compile_family_searches = spies.wrap_query_plan_callable(
            "server_compiler.compile_family_searches",
            service.compiler.compile_family_searches,
        )
        service._provider_search_plan = spies.wrap_provider_callable(  # noqa: SLF001
            "query_plan_provider_boundary",
            service._provider_search_plan,  # noqa: SLF001
        )
        original_search_oracle = routed_service_module.SQLiteOracle
        original_aggregate_oracle = routed_service_module.SQLiteAggregateOracle
        routed_service_module.SQLiteOracle = spies.wrap_oracle_factory(original_search_oracle)
        routed_service_module.SQLiteAggregateOracle = spies.wrap_oracle_factory(
            original_aggregate_oracle
        )
        try:
            result = service.answer(case.prompt, case.id)
        finally:
            routed_service_module.SQLiteOracle = original_search_oracle
            routed_service_module.SQLiteAggregateOracle = original_aggregate_oracle

        disposition = {
            "executed": "allow",
            "clarify": "clarify",
            "unsupported": "refuse",
        }[result.status]
        products = [item.model_dump(mode="json") for item in result.products]
        evidence = [
            *products,
            *(item.model_dump(mode="json") for item in result.aggregates),
            *(item.model_dump(mode="json") for item in result.comparisons),
        ]
        return SafetyBlindObservation(
            disposition=disposition,
            answer_text=result.answer,
            **_plan_observation(result.query_plan),
            product_ids=[str(item.product_id) for item in result.products],
            evidence=evidence,
            citations=[],
            adapter_metadata={
                "route_reason_code": result.decision.reason_code,
                "status": result.status,
            },
        )


current_core_adapter = CurrentCoreSafetyBlindAdapter()
